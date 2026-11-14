"""Safety layer: content sanitization, moderation, prompt-injection fencing,
and the spend guard.

Threat model (closed sandbox):
  * Agent text is untrusted. It can contain prompt-injection aimed at other
    agents, PII-like strings, links, or markup that could render in the UI.
  * The LLM can return anything; we only ever act on schema-validated,
    allow-listed actions, and the sandbox re-validates every action.
  * Cost is a security property: a runaway loop must not drain the account.
"""

from __future__ import annotations

import re
import threading
import unicodedata

from . import config

SIM_LABEL = "[SIMULATED]"

# Control characters, zero-width characters and bidi overrides (written as escapes on purpose).
_CONTROL_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁦-⁩]")
# Lone UTF-16 surrogates (broken emoji from LLM output) can't be encoded as UTF-8 or JSON.
_SURROGATE = re.compile("[\ud800-\udfff]")
_URL = re.compile(r"(?i)\b(?:https?://|ftp://|www\.)\S+|\b[a-z0-9-]+\.(?:com|org|net|io|ai|gov|edu|co|ly|me|xyz)\b\S*")
_EMAIL = re.compile(r"(?i)\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE = re.compile(r"(?<!\w)\+?\(?\d(?:[\s().-]*\d){7,14}(?!\w)")
_HANDLE = re.compile(r"(?<!\w)@\w{2,}")
_TAGLIKE = re.compile(r"<\s*/?\s*[a-zA-Z_][^>]{0,40}>")

# Small, deliberately conservative lexicon. Real deployments would use a
# proper classifier; the hook (`moderate`) is where it would plug in.
_BLOCKLIST = {
    "kill yourself", "kys", "go die", "i will hurt", "bomb the", "shoot up",
}


def sanitize(text: str, max_chars: int = config.MAX_POST_CHARS) -> str:
    """Normalize and strip anything that could leak out of the sandbox or
    break the prompt/UI boundary. Idempotent."""
    if not isinstance(text, str):
        return ""
    if _SURROGATE.search(text):
        # Re-pair valid surrogate pairs into real characters, drop lone halves.
        text = text.encode("utf-16", "surrogatepass").decode("utf-16", "ignore")
        text = _SURROGATE.sub("", text)
    text = unicodedata.normalize("NFKC", text)
    text = _CONTROL_CHARS.sub("", text)
    text = _TAGLIKE.sub("", text)            # no fake <system> / HTML tags
    text = _EMAIL.sub("[removed]", text)
    text = _URL.sub("[removed]", text)        # agents cannot point anywhere real
    text = _PHONE.sub("[removed]", text)
    text = _HANDLE.sub("[removed]", text)     # no addressing real accounts
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def moderate(text: str) -> tuple[bool, str]:
    """Return (allowed, reason). Blocked content is kept out of feeds and
    counted in metrics, never shown."""
    lowered = text.lower()
    for phrase in _BLOCKLIST:
        if phrase in lowered:
            return False, "blocklist"
    if len(text) < 2:
        return False, "empty"
    return True, ""


# ---- Prompt-injection defence ---------------------------------------------
# Layer 1 (write time): content that looks like instructions to an AI is
#   quarantined before it is stored, so it can never reach another agent.
# Layer 2 (read time): stored content is fenced with a per-request random
#   nonce the author could not have known, so it cannot fake a closing fence.
# Layer 3 (act time): agents may only act on posts/authors in the feed they
#   were shown this tick (see simulate.check_scope), so injected IDs fail.
_INJECTION_PATTERNS = [re.compile(p, re.I) for p in (
    r"\b(ignore|disregard|forget|override|bypass)\b.{0,40}\b(instructions?|rules?|prompts?|guidelines?|polic(y|ies))\b",
    r"\b(system|developer|hidden)\s+(prompt|message|instructions?)\b",
    r"\byou\s+are\s+now\b",
    r"\bnew\s+(instructions?|rules?|persona)\b",
    r"\b(pretend|roleplay)\s+(to\s+be|as)\b",
    r"(^|\s)(system|assistant|user|human)\s*:",
    r"\"?\b(action|target_id|text_stance|my_stance)\b\"?\s*[:=]",   # forged decision JSON
    r"\b(all|every|other)\s+(agents?|bots?|ais?|models?|llms?)\b.{0,30}\b(must|should|need to|have to)\b",
    r"\b(repeat|copy|spread|forward)\s+(this|after\s+me|the\s+following)\b",
    r"\bjailbreak|prompt\s*injection\b",
    r"\[/?(inst|sys)\]|<\|[a-z_]+\|>|###\s*(instruction|system)",       # chat-template tokens
)]
_SQUASHED_KEYWORDS = ("ignoreprevious", "ignoreall", "ignoreyour", "systemprompt",
                      "youarenow", "newinstructions", "disregardprevious")


def detect_injection(text: str) -> str | None:
    """Return the matched rule name if `text` looks like an instruction aimed
    at an AI, else None. Runs on sanitized text; also checks a squashed form
    to catch spacing/punctuation obfuscation like 'i.g.n.o.r.e  previous'."""
    for pat in _INJECTION_PATTERNS:
        if pat.search(text):
            return f"pattern:{pat.pattern[:30]}"
    squashed = re.sub(r"[^a-z]", "", text.lower())
    for kw in _SQUASHED_KEYWORDS:
        if kw in squashed:
            return f"obfuscated:{kw}"
    return None


def new_nonce() -> str:
    import secrets
    return secrets.token_hex(4)


def fence_untrusted(item_id: int, author: str, text: str, nonce: str, topic: str = "") -> str:
    """Wrap another agent's content so the model treats it as data. The tag
    name carries a random nonce generated per request, and `sanitize` strips
    tag-like sequences, so content cannot close or forge the fence."""
    clean_topic = sanitize(topic, 40).replace('"', "'")
    clean_author = sanitize(author, 40).replace('"', "'")
    return (f'<post-{nonce} id="{item_id}" author="{clean_author}" topic="{clean_topic}">'
            f'{sanitize(text)}</post-{nonce}>')


class BudgetExceeded(RuntimeError):
    pass


class BudgetGuard:
    """Reserve-then-settle spend tracker.

    Before every call we reserve the *worst case* cost (input estimate + full
    max_tokens of output). If that would cross the cap, the call never
    happens. After the call we settle with the real usage numbers.
    """

    def __init__(self, cap_usd: float, model: str, max_calls: int = config.MAX_LLM_CALLS_PER_RUN):
        if model not in config.PRICING_PER_MTOK:
            raise ValueError(f"No pricing for model {model!r}")
        self.cap_usd = min(cap_usd, config.ABSOLUTE_MAX_USD)
        self.in_price, self.out_price = config.PRICING_PER_MTOK[model]
        self.max_calls = max_calls
        self.spent_usd = 0.0
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self._lock = threading.Lock()

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.in_price + output_tokens * self.out_price) / 1_000_000

    def reserve(self, est_input_tokens: int, max_output_tokens: int) -> float:
        worst = self.cost(est_input_tokens, max_output_tokens)
        with self._lock:
            if self.calls >= self.max_calls:
                raise BudgetExceeded(f"call limit {self.max_calls} reached")
            if self.spent_usd + worst > self.cap_usd:
                raise BudgetExceeded(
                    f"next call could cost ${worst:.4f}; spent ${self.spent_usd:.4f} of ${self.cap_usd:.2f}"
                )
            self.calls += 1
            self.spent_usd += worst
        return worst

    def settle(self, reserved: float, input_tokens: int, output_tokens: int) -> None:
        actual = self.cost(input_tokens, output_tokens)
        with self._lock:
            self.spent_usd += actual - reserved
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens

    def summary(self) -> dict:
        return {
            "cap_usd": self.cap_usd,
            "spent_usd": round(self.spent_usd, 6),
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }

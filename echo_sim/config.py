"""Central configuration and hard safety limits.

Hard limits are constants in code on purpose: CLI flags and env vars can only
lower them, never raise them, so a typo cannot launch a 10,000-agent run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

# ---- Hard ceilings (edit code, not flags, to change these) -----------------
MAX_AGENTS = 100
MAX_TICKS = 50
MAX_LLM_CALLS_PER_RUN = 3000
ABSOLUTE_MAX_USD = 10.00          # nothing can exceed this, even via env
MAX_POST_CHARS = 400
MAX_FEED_ITEMS = 6                # posts an agent sees per tick
MAX_MEMORY_ITEMS = 8              # recent memories fed back into the prompt
MAX_OUTPUT_TOKENS = 1024          # per agent decision
LLM_TIMEOUT_SECONDS = 30.0
LLM_MAX_RETRIES = 2

# Haiku 5.5 first-party pricing, USD per million tokens (prompts <= 100K).
# Used only for the local budget guard; keep in sync with current pricing.
PRICING_PER_MTOK = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}

# Fictional setting, NOT a topic: agents decide what to talk about themselves.
# Keeping the world fictional avoids real politics, real people and real-world harm.
SETTING = (
    "Maplebrook is a fictional small town with its own local social platform, "
    "where residents post about whatever is on their mind."
)
MAX_OPINIONS_PER_AGENT = 30       # most recent topics an agent holds opinions on


LOCAL_TIMEOUT_SECONDS = 120.0     # local models on a laptop GPU are slower
LOCAL_MAX_OUTPUT_TOKENS = 400


def ollama_host() -> str:
    """Local LLM endpoint. Must be loopback: agent prompts never leave this machine."""
    from urllib.parse import urlparse
    host = os.getenv("ECHO_OLLAMA_HOST", "http://127.0.0.1:11434").strip()
    parsed = urlparse(host)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(f"ECHO_OLLAMA_HOST must be a loopback http URL, got {host!r}")
    return host


def ollama_model() -> str:
    return os.getenv("ECHO_OLLAMA_MODEL", "qwen2.5:3b").strip()


@dataclass(frozen=True)
class Settings:
    model: str
    hard_cap_usd: float
    api_key_present: bool


def load_settings() -> Settings:
    model = os.getenv("ECHO_MODEL", "claude-haiku-5-5").strip()
    if model not in PRICING_PER_MTOK:
        raise ValueError(
            f"ECHO_MODEL={model!r} has no pricing entry; add it to PRICING_PER_MTOK "
            "so the budget guard can account for it."
        )
    try:
        cap = float(os.getenv("ECHO_MAX_USD_HARD_CAP", "2.00"))
    except ValueError as exc:
        raise ValueError("ECHO_MAX_USD_HARD_CAP must be a number") from exc
    cap = max(0.0, min(cap, ABSOLUTE_MAX_USD))
    # Never store or print the key itself — only whether it exists.
    return Settings(
        model=model,
        hard_cap_usd=cap,
        api_key_present=bool(os.getenv("ANTHROPIC_API_KEY")),
    )

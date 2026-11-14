"""Emergent topics: agents name their own topics; this module turns free-text
labels into a clean, bounded vocabulary.

* normalize(): sanitize + lowercase + strip to a short safe label, or None if
  the label is unusable or looks like an injection attempt.
* TopicRegistry.resolve(): merges near-duplicates ("the community garden" ->
  "community garden") so trends are countable, and caps how many distinct
  topics a world can hold.
"""

from __future__ import annotations

import re

from . import safety

MAX_TOPICS = 150
MAX_TOPIC_CHARS = 40
FALLBACK = "general chat"
_STOP = {"the", "a", "an", "of", "for", "in", "on", "at", "to", "and", "our", "my", "about",
         "new", "local", "maplebrook", "maplebrooks", "town", "towns"}


def normalize(raw: str) -> str | None:
    if not isinstance(raw, str):
        return None
    text = safety.sanitize(raw, 80)
    if safety.detect_injection(text) or not safety.moderate(text + " ok")[0]:
        return None
    text = text.lower().replace("#", " ").replace("_", " ")
    text = re.sub(r"[^a-z0-9' -]", " ", text)
    words = [w.strip("'-") for w in text.split()]
    words = [w for w in words if w][:5]
    label = " ".join(words)[:MAX_TOPIC_CHARS].strip()
    return label if len(label) >= 3 and "removed" not in label else None


def _key(label: str) -> frozenset[str]:
    words = {w[:-1] if len(w) > 4 and w.endswith("s") else w for w in label.split()}
    core = words - _STOP
    return frozenset(core or words)


def _similar(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    if a <= b or b <= a:
        return 0.9
    return len(a & b) / len(a | b)


class TopicRegistry:
    """Canonical topic labels with post counts."""

    def __init__(self, counts: dict[str, int] | None = None):
        self.counts: dict[str, int] = dict(counts or {})
        self._keys = {t: _key(t) for t in self.counts}

    def resolve(self, raw: str) -> str:
        label = normalize(raw) or FALLBACK
        if label in self.counts:
            return label
        k = _key(label)
        best, score = None, 0.0
        for t, tk in self._keys.items():
            s = _similar(k, tk)
            if s > score:
                best, score = t, s
        if best is not None and score >= 0.6:
            return best
        if len(self.counts) >= MAX_TOPICS:
            return best if best is not None and score > 0 else FALLBACK
        self.counts[label] = 0
        self._keys[label] = k
        return label

    def add_post(self, topic: str) -> None:
        if topic not in self.counts:
            self._keys[topic] = _key(topic)
        self.counts[topic] = self.counts.get(topic, 0) + 1

    def top(self, n: int = 10) -> list[tuple[str, int]]:
        return sorted(((t, c) for t, c in self.counts.items() if c), key=lambda x: -x[1])[:n]

"""Agent emotions: a small valence/arousal model, separate from opinions.

* valence  -1 (bad) .. +1 (good)   how the agent feels
* arousal   0 (calm) .. 1 (fired up)  how activated it is

Emotions change with what an agent sees (agreeable vs. opposing posts) and with
feedback on its own posts (votes, replies, new followers), and decay back to a
persona baseline. They feed back into behaviour: aroused agents act more often
and reply more, unhappy agents downvote more, and the LLM writer is told how the
agent feels so the tone matches.
"""

from __future__ import annotations

from dataclasses import dataclass

BASELINE_VALENCE = {
    "enthusiastic": 0.3, "friendly": 0.2, "funny": 0.2, "nostalgic": 0.05, "calm and factual": 0.05,
    "data-driven": 0.0, "skeptical": -0.05, "sarcastic": -0.05, "blunt": -0.05, "anxious": -0.15,
}
BASELINE_AROUSAL = 0.3
DECAY = 0.15

LABELS = ["happy", "excited", "calm", "curious", "neutral", "anxious", "sad", "angry"]
EMOJI = {"happy": "🙂", "excited": "🤩", "calm": "😌", "curious": "🤔", "neutral": "😐",
         "anxious": "😟", "sad": "😢", "angry": "😠"}


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(x)))


@dataclass
class Emotion:
    valence: float = 0.0
    arousal: float = BASELINE_AROUSAL

    @property
    def label(self) -> str:
        v, a = self.valence, self.arousal
        if v >= 0.3:
            return "excited" if a >= 0.55 else "happy"
        if v <= -0.3:
            return "angry" if a >= 0.55 else "sad"
        if a >= 0.55:
            return "anxious" if v < 0 else "curious"
        if a < 0.25:
            return "calm"
        return "neutral"

    def clamp(self) -> "Emotion":
        self.valence = round(_clamp(self.valence, -1, 1), 4)
        self.arousal = round(_clamp(self.arousal, 0, 1), 4)
        return self

    def activity_factor(self) -> float:
        """Multiplier on how often an agent acts: fired-up agents post more."""
        return 0.6 + 0.8 * self.arousal

    def to_dict(self) -> dict:
        return {"valence": self.valence, "arousal": self.arousal, "label": self.label,
                "emoji": EMOJI[self.label]}


def baseline(style: str) -> float:
    return BASELINE_VALENCE.get(style, 0.0)


def expose(emo: Emotion, feed, opinions: dict[str, float]) -> None:
    """Reading the feed: agreeable posts lift the mood, opposing strong posts
    lower it and raise arousal (the 'outrage' effect)."""
    for p in feed:
        held = opinions.get(p.topic)
        strength = abs(p.stance)
        if held is None:
            emo.arousal += 0.01 * strength
            continue
        agree = 1 - abs(p.stance - held) / 2          # 0 opposite .. 1 identical
        emo.valence += 0.03 * (agree - 0.6) * 2      # slight negativity bias, as in real feeds
        emo.arousal += 0.06 * strength * (1.0 if agree < 0.4 else 0.25)
    emo.clamp()


def feedback(emo: Emotion, inbox: dict) -> None:
    """Notifications about the agent's own content."""
    up, down = inbox.get("up", 0), inbox.get("down", 0)
    agree, disagree = inbox.get("agree", 0), inbox.get("disagree", 0)
    followers = inbox.get("followers", 0)
    emo.valence += min(0.15, 0.03 * up + 0.04 * agree + 0.06 * followers) - min(0.3, 0.07 * down + 0.08 * disagree)
    emo.arousal += min(0.3, 0.07 * (down + disagree)) + min(0.1, 0.02 * (up + agree + followers))
    emo.clamp()


def decay(emo: Emotion, base_valence: float) -> None:
    emo.valence += (base_valence - emo.valence) * DECAY
    emo.arousal += (BASELINE_AROUSAL - emo.arousal) * DECAY
    emo.clamp()

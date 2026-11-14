"""Community-aware feed ranking (the experimental variable).

Same three policies as Echo, plus Socialverse signals: joined communities and
followed authors. Only the most recent CANDIDATE_WINDOW posts are ranked so a
large world stays fast.
"""

from __future__ import annotations

from echo_sim.sandbox import Post

CANDIDATE_WINDOW = 400
POLICIES = ("chronological", "engagement", "personalized")


def _engagement(p: Post, now: int) -> float:
    age = max(0, now - p.tick)
    return (p.score + 2 * p.replies + 1) / (1 + age) ** 1.5


def _personalized(p: Post, opinions: dict, following: set[int], communities: set[int], now: int) -> float:
    held = opinions.get(p.topic)
    interest = 1.0 if held is not None else 0.0
    similarity = 1 - abs(p.stance - held) / 2 if held is not None else 0.5
    followed = 1.0 if p.author in following else 0.0
    member = 1.0 if p.community is not None and p.community in communities else 0.0
    recency = 1 / (1 + max(0, now - p.tick))
    return 2 * similarity + interest + followed + member + recency


def build_feed(policy: str, posts: list[Post], viewer: int, opinions: dict, following: set[int],
               communities: set[int], now: int, k: int) -> list[Post]:
    if policy not in POLICIES:
        raise ValueError("unknown policy")
    cand = [p for p in posts[-CANDIDATE_WINDOW:] if p.visible and p.author != viewer]
    if policy == "chronological":
        ranked = sorted(cand, key=lambda p: p.id, reverse=True)
    elif policy == "engagement":
        ranked = sorted(cand, key=lambda p: _engagement(p, now), reverse=True)
    else:
        ranked = sorted(cand, key=lambda p: _personalized(p, opinions, following, communities, now),
                        reverse=True)
    return ranked[:k]

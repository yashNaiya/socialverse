"""Swappable feed-ranking policies — the independent variable of experiments."""

from __future__ import annotations

from typing import Callable

from .sandbox import Post

# (posts, viewer, viewer_opinions{topic: -1..1}, following, now) -> ranked posts
Ranker = Callable[[list[Post], int, dict, set[int], int], list[Post]]


def chronological(posts, viewer, opinions, following, now):
    return sorted(posts, key=lambda p: p.id, reverse=True)


def engagement(posts, viewer, opinions, following, now):
    """Reddit-'hot'-like: votes + replies, decayed by age."""
    def score(p: Post) -> float:
        age = max(0, now - p.tick)
        return (p.score + 2 * p.replies + 1) / (1 + age) ** 1.5
    return sorted(posts, key=score, reverse=True)


def personalized(posts, viewer, opinions, following, now):
    """Filter-bubble recommender: boosts topics the viewer already engages
    with, posts that agree with the viewer's opinion on that topic, and
    followed authors. Expected to raise polarization."""
    def score(p: Post) -> float:
        held = opinions.get(p.topic)
        interest = 1.0 if held is not None else 0.0
        similarity = 1 - abs(p.stance - held) / 2 if held is not None else 0.5
        followed = 1.0 if p.author in following else 0.0
        recency = 1 / (1 + max(0, now - p.tick))
        return 2 * similarity + interest + followed + recency
    return sorted(posts, key=score, reverse=True)


POLICIES: dict[str, Ranker] = {
    "chronological": chronological,
    "engagement": engagement,
    "personalized": personalized,
}


def build_feed(policy: str, posts: list[Post], viewer: int, opinions: dict,
               following: set[int], now: int, k: int) -> list[Post]:
    candidates = [p for p in posts if p.author != viewer and p.visible]
    return POLICIES[policy](candidates, viewer, opinions, following, now)[:k]

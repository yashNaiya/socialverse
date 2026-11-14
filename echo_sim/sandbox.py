"""The closed social platform. In-process only: no network listener, no
outbound requests. Every action is validated here regardless of what the
LLM said, so a confused or injected agent can only do allowed things."""

from __future__ import annotations

from dataclasses import dataclass, field

from . import safety
from .topics import TopicRegistry


@dataclass
class Post:
    id: int
    author: int
    text: str
    stance: float                 # author's position on the post's topic, -1 critical .. +1 supportive
    tick: int
    parent: int | None = None     # None = top-level post, else reply to post id
    score: int = 0
    replies: int = 0
    visible: bool = True
    voters: set[int] = field(default_factory=set)
    topic: str = "general chat"   # chosen by the author (replies inherit the thread's topic)
    agreement: float | None = None  # replies only: -1 disagrees .. +1 agrees with the parent
    community: int | None = None    # Socialverse: community the thread belongs to (None = town square)
    format: str = "post"            # Socialverse: meme, hot_take, gossip, news_share, ...
    happening: int | None = None    # Socialverse: the news/trend/meme this post reacts to


class ActionError(ValueError):
    """Raised when an agent attempts something the platform does not allow."""


class Sandbox:
    def __init__(self, n_agents: int, max_actions_per_tick: int = 1):
        self.n_agents = n_agents
        self.posts: dict[int, Post] = {}
        self.follows: set[tuple[int, int]] = set()         # (follower, followee)
        self.interactions: list[tuple[int, int, str, int]] = []  # (src, dst, kind, tick)
        self.topics = TopicRegistry()
        self.moderated = 0
        self.quarantined = 0          # suspected prompt-injection attempts
        self.rejected_actions = 0
        self.persisted_interactions = 0   # how many interactions are already saved to the DB
        self._next_id = 1
        self._max_actions = max_actions_per_tick
        self._actions_this_tick: dict[int, int] = {}

    # ---- lifecycle --------------------------------------------------------
    def start_tick(self) -> None:
        self._actions_this_tick.clear()

    def _check_agent(self, agent: int) -> None:
        if not (0 <= agent < self.n_agents):
            raise ActionError("unknown agent")
        used = self._actions_this_tick.get(agent, 0)
        if used >= self._max_actions:
            raise ActionError("rate limit")
        self._actions_this_tick[agent] = used + 1

    def _get_visible(self, post_id: int) -> Post:
        post = self.posts.get(post_id)
        if post is None or not post.visible:
            raise ActionError("no such post")
        return post

    def topic_of(self, post_id: int) -> str | None:
        p = self.posts.get(post_id)
        return p.topic if p and p.visible else None

    # ---- actions ----------------------------------------------------------
    def create_post(self, agent: int, text: str, stance: float, tick: int, parent: int | None = None,
                    topic: str = "general chat", agreement: float | None = None) -> Post | None:
        self._check_agent(agent)
        parent_post = self._get_visible(parent) if parent is not None else None
        clean = safety.sanitize(text)
        allowed, _ = safety.moderate(clean)
        if not allowed:
            self.moderated += 1
            return None
        if safety.detect_injection(clean):
            # Never stored, never shown to any agent: stops injection "worms"
            # that try to propagate from one agent's output to the next prompt.
            self.quarantined += 1
            return None
        if parent_post is not None:
            topic = parent_post.topic                   # replies stay on the thread's topic
        else:
            topic = self.topics.resolve(topic)          # sanitized, merged, capped
            agreement = None
        clamp = lambda x: max(-1.0, min(1.0, float(x)))  # noqa: E731
        post = Post(id=self._next_id, author=agent, text=clean, stance=clamp(stance), tick=tick,
                    parent=parent, topic=topic,
                    agreement=None if agreement is None else clamp(agreement))
        self._next_id += 1
        self.posts[post.id] = post
        self.topics.add_post(topic)
        if parent_post is not None:
            parent_post.replies += 1
            if parent_post.author != agent:
                self.interactions.append((agent, parent_post.author, "comment", tick))
        return post

    def vote(self, agent: int, post_id: int, value: int, tick: int) -> None:
        self._check_agent(agent)
        post = self._get_visible(post_id)
        if value not in (1, -1):
            raise ActionError("bad vote")
        if post.author == agent:
            raise ActionError("cannot vote on own post")
        if agent in post.voters:
            raise ActionError("already voted")
        post.voters.add(agent)
        post.score += value
        self.interactions.append((agent, post.author, "upvote" if value > 0 else "downvote", tick))

    def follow(self, agent: int, target: int, tick: int) -> None:
        self._check_agent(agent)
        if not (0 <= target < self.n_agents) or target == agent:
            raise ActionError("bad follow target")
        if (agent, target) in self.follows:
            raise ActionError("already following")
        self.follows.add((agent, target))
        self.interactions.append((agent, target, "follow", tick))

    # ---- queries ----------------------------------------------------------
    def visible_posts(self) -> list[Post]:
        return [p for p in self.posts.values() if p.visible]

    def following(self, agent: int) -> set[int]:
        return {b for (a, b) in self.follows if a == agent}

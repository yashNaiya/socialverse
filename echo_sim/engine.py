"""Shared simulation step logic, used by both experiment runs (simulate.py)
and persistent worlds (world.py)."""

from __future__ import annotations

import sys
import time

from . import config, ranking, safety
from .brains import ClaudeBrain, Decision, MockBrain, OllamaBrain
from .sandbox import ActionError, Sandbox

MAX_STANCE_STEP = 0.5   # cap per-round opinion change so one reply can't flip an agent


def make_brain(mode: str, seed: int, *, max_usd: float = 0.25, yes: bool = False, est_calls: int = 100):
    """Build the decision-maker for a mode. Returns (brain, budget_or_None)."""
    if mode == "ollama":
        brain = OllamaBrain(config.ollama_model(), config.ollama_host())
        brain.check_ready()
        print(f"Local model {brain.model} via {config.ollama_host()} (free, on-device).")
        return brain, None
    if mode == "claude":
        settings = config.load_settings()
        cap = min(max_usd, settings.hard_cap_usd)
        budget = safety.BudgetGuard(cap, settings.model)
        worst = est_calls * budget.cost(2500, config.MAX_OUTPUT_TOKENS)
        print(f"Model {settings.model}. Spend cap ${cap:.2f} (worst case without cap ${worst:.2f}).")
        if not yes:
            if not sys.stdin.isatty() or input("Proceed? [y/N] ").strip().lower() != "y":
                print("Aborted.")
                sys.exit(1)
        return ClaudeBrain(settings.model, budget), budget
    return MockBrain(seed), None


def check_scope(decision: Decision, feed) -> None:
    """Least privilege: an agent may only act on what it was actually shown.
    Text injected into a post can mention any id; acting on it fails here."""
    if decision.action in ("comment", "upvote", "downvote"):
        if decision.target_id not in {p.id for p in feed}:
            raise ActionError("target not in your feed")
    elif decision.action == "follow":
        if decision.target_id not in {p.author for p in feed}:
            raise ActionError("author not in your feed")


def apply(decision: Decision, agent: int, sb: Sandbox, tick: int,
          opinion: float | None = None) -> tuple[str, int | None]:
    """Execute a validated decision. Returns (memory line, new post id or None)."""
    d = decision
    if d.action == "idle":
        return f"round {tick}: you scrolled without acting", None
    if d.action in ("post", "comment"):
        if d.action == "comment":
            # A reply's position on the topic is the replier's own opinion;
            # text_stance is how much they agree with the parent.
            post = sb.create_post(agent, d.text, opinion if opinion is not None else 0.0, tick,
                                  parent=d.target_id, agreement=d.text_stance)
        else:
            post = sb.create_post(agent, d.text, d.text_stance, tick, topic=d.topic or "general chat")
        if post is None:
            return f"round {tick}: your {d.action} was removed by moderation", None
        return (f"round {tick}: you wrote ({d.action} #{post.id}, topic '{post.topic}'): "
                f"{post.text[:110]}"), post.id
    if d.action in ("upvote", "downvote"):
        sb.vote(agent, d.target_id, 1 if d.action == "upvote" else -1, tick)
        return f"round {tick}: you {d.action}d post #{d.target_id}", None
    if d.action == "follow":
        sb.follow(agent, d.target_id, tick)
        return f"round {tick}: you followed agent {d.target_id}", None
    raise ActionError("unknown action")


def update_opinion(opinions: dict[str, float], topic: str, value: float) -> float:
    """First contact with a topic forms an opinion; later contacts move it by
    at most MAX_STANCE_STEP. Keeps only the most recent topics (recency order)."""
    old = opinions.pop(topic, None)
    if old is None:
        new = value
    else:
        new = old + max(-MAX_STANCE_STEP, min(MAX_STANCE_STEP, value - old))
    opinions[topic] = round(max(-1.0, min(1.0, new)), 4)
    while len(opinions) > config.MAX_OPINIONS_PER_AGENT:
        opinions.pop(next(iter(opinions)))
    return opinions[topic] - (old if old is not None else opinions[topic])


def take_turn(brain, policy: str, sb: Sandbox, persona, opinions: list[dict[str, float]],
              memory: list[list[str]], handles: dict[int, str], tick: int, t0: float) -> dict:
    """One agent reads its feed, decides, and acts. Mutates sandbox, opinions
    and memory; returns an event record describing what happened."""
    a = persona.id
    feed = ranking.build_feed(policy, sb.visible_posts(), a, opinions[a],
                              sb.following(a), tick, config.MAX_FEED_ITEMS)
    trending = [t for t, _ in sb.topics.top(5)]
    d = brain.decide(persona, opinions[a], memory[a], feed, handles, trending).clamped()

    # Which topic is this action about? New posts name their own (resolved by the
    # registry), replies and votes are about the target thread. Follows aren't.
    feed_ids = {p.id: p for p in feed}
    if d.action == "post":
        topic = sb.topics.resolve(d.topic or "general chat")
        d = d.model_copy(update={"topic": topic})
    elif d.action in ("comment", "upvote", "downvote") and d.target_id in feed_ids:
        topic = feed_ids[d.target_id].topic
    else:
        topic = None
    step = update_opinion(opinions[a], topic, d.my_stance) if topic else 0.0

    event = {"tick": tick, "agent": a, "action": d.action, "target": d.target_id,
             "ok": True, "note": "", "post_id": None, "stance_change": round(step, 3),
             "topic": topic or "", "t": round(time.time() - t0, 2)}
    q_before, m_before = sb.quarantined, sb.moderated
    try:
        check_scope(d, feed)
        line, event["post_id"] = apply(d, a, sb, tick, opinions[a].get(topic) if topic else None)
        memory[a].append(line)
        if sb.quarantined > q_before:
            event.update(ok=False, note="quarantined: looked like prompt injection")
        elif sb.moderated > m_before:
            event.update(ok=False, note="removed by moderation")
    except ActionError as e:
        sb.rejected_actions += 1
        memory[a].append(f"round {tick}: your {d.action} failed ({e})")
        event.update(ok=False, note=f"blocked: {e}")
    memory[a][:] = memory[a][-config.MAX_MEMORY_ITEMS:]
    return event


def is_notable(event: dict) -> bool:
    return event["action"] != "idle" or abs(event["stance_change"]) > 0.05

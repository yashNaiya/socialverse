"""Experiment runs: a fresh, isolated population every time (for controlled
comparisons between feed policies). For a platform that keeps growing across
sessions, use `python -m echo_sim.world` instead.

    python -m echo_sim.simulate --mode mock --agents 30 --ticks 15 --policy engagement
    python -m echo_sim.simulate --mode ollama --agents 10 --ticks 5 --policy personalized
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import analysis, config, engine, personas, ranking, safety
from .engine import apply, check_scope  # noqa: F401  (re-exported for callers/tests)
from .sandbox import Sandbox

log = logging.getLogger("echo")


def _bounded_int(lo: int, hi: int):
    def parse(v: str) -> int:
        n = int(v)
        if not lo <= n <= hi:
            raise argparse.ArgumentTypeError(f"must be between {lo} and {hi}")
        return n
    return parse


def _run_id(v: str) -> str:
    # Used as a folder name: forbid path separators, dots and anything exotic.
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", v):
        raise argparse.ArgumentTypeError("name may only contain letters, digits, - and _")
    return v


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Echo experiment run (fresh population each time)")
    p.add_argument("--mode", choices=["mock", "ollama", "claude"], default="mock",
                   help="mock = free rule-based baseline; ollama = free local LLM agents; "
                        "claude = hosted LLM agents (costs money)")
    p.add_argument("--agents", type=_bounded_int(2, config.MAX_AGENTS), default=20)
    p.add_argument("--ticks", type=_bounded_int(1, config.MAX_TICKS), default=10)
    p.add_argument("--policy", choices=sorted(ranking.POLICIES), default="chronological")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--max-usd", type=float, default=0.25,
                   help="spend cap for this run (clamped to ECHO_MAX_USD_HARD_CAP)")
    p.add_argument("--yes", action="store_true", help="skip the cost confirmation prompt")
    p.add_argument("--out", default="runs")
    p.add_argument("--run-id", type=_run_id, default=None,
                   help="name of the run folder (letters, digits, - and _ only)")
    p.add_argument("--delay", type=float, default=0.0,
                   help="seconds to pause after each action, so a live viewer can follow along (max 5)")
    args = p.parse_args(argv)
    args.delay = max(0.0, min(args.delay, 5.0))
    return args


def atomic_write_json(path: Path, data: dict) -> None:
    """Write via temp file + rename so a reader never sees a half-written file.
    On Windows the rename can briefly fail while the dashboard is reading."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
    log.warning("could not update %s (file busy)", path.name)


def post_dict(p) -> dict:
    return {"id": p.id, "author": p.author, "parent": p.parent, "tick": p.tick,
            "score": p.score, "replies": p.replies, "stance": p.stance, "text": p.text,
            "topic": p.topic, "agreement": p.agreement}


def _live_snapshot(status, args, tick, people, opinions, sb, events, history, started) -> dict:
    followers = [0] * args.agents
    for _, b in sb.follows:
        followers[b] += 1
    post_count = [0] * args.agents
    for p in sb.posts.values():
        post_count[p.author] += 1
    comm = analysis.communities_of(args.agents, sb)
    return {
        "label": safety.SIM_LABEL, "synthetic": True, "topic": config.SETTING,
        "status": status, "tick": tick,
        "config": {"mode": args.mode, "agents": args.agents, "ticks": args.ticks,
                   "policy": args.policy, "seed": args.seed},
        "started": started, "updated": time.time(),
        "agents": [{"id": p.id, "handle": p.handle, "job": p.job, "community": comm[p.id],
                    "opinions": dict(list(opinions[p.id].items())[-5:]),
                    "followers": followers[p.id], "posts": post_count[p.id]} for p in people],
        "posts": [post_dict(p) for p in list(sb.posts.values())[-400:]],
        "events": events[-150:],
        "metrics": history,
        "counters": {"moderated": sb.moderated, "quarantined": sb.quarantined,
                     "rejected": sb.rejected_actions},
        "interactions": sb.interactions[-3000:],
    }


class _StopRequested(Exception):
    pass


def run(args: argparse.Namespace) -> Path:
    rng = random.Random(args.seed)
    people = personas.generate(args.agents, args.seed)
    opinions: list[dict[str, float]] = [{} for _ in people]
    handles = {p.id: p.handle for p in people}
    memory: list[list[str]] = [[] for _ in people]
    sb = Sandbox(args.agents)
    brain, budget = engine.make_brain(args.mode, args.seed, max_usd=args.max_usd, yes=args.yes,
                                      est_calls=args.agents * args.ticks)

    name = args.run_id or (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                           + f"_{args.mode}_{args.policy}_s{args.seed}")
    out_dir = Path(args.out) / name
    out_dir.mkdir(parents=True, exist_ok=False)
    live_path, stop_flag = out_dir / "live.json", out_dir / "STOP"
    history = [{"tick": 0, **analysis.metrics(args.agents, sb, opinions)}]
    events: list[dict] = []
    stopped_reason = "completed"
    t0 = time.time()

    def publish(status: str, tick: int) -> None:
        atomic_write_json(live_path, _live_snapshot(status, args, tick, people, opinions,
                                                    sb, events, history, t0))

    publish("running", 0)
    try:
        for tick in range(1, args.ticks + 1):
            sb.start_tick()
            order = list(range(args.agents))
            rng.shuffle(order)
            for a in order:
                if stop_flag.exists():
                    raise _StopRequested()
                if rng.random() > people[a].activity:
                    continue
                ev = engine.take_turn(brain, args.policy, sb, people[a], opinions, memory, handles, tick, t0)
                if engine.is_notable(ev):
                    events.append(ev)
                publish("running", tick)
                if args.delay:
                    time.sleep(args.delay)
            history.append({"tick": tick, **analysis.metrics(args.agents, sb, opinions)})
            m = history[-1]
            spent = f" spent=${budget.spent_usd:.4f}" if budget else ""
            print(f"tick {tick:>3}: posts={m['posts']:>4} topics={m['n_topics']:>3} "
                  f"top='{m['top_topic']}' modularity={m['modularity']} "
                  f"homophily={m['opinion_homophily']}{spent}")
            publish("running", tick)
    except safety.BudgetExceeded as e:
        stopped_reason = f"budget: {e}"
        print(f"Stopped early: {e}")
    except _StopRequested:
        stopped_reason = "stopped by user"
        print("Stopped by user.")
    except KeyboardInterrupt:
        stopped_reason = "interrupted"
    except Exception as e:                      # surface crashes to the live viewer
        stopped_reason = f"error: {type(e).__name__}: {str(e)[:200]}"
        publish(stopped_reason, len(history) - 1)
        raise

    publish("completed" if stopped_reason == "completed" else stopped_reason, len(history) - 1)
    data = {
        "label": safety.SIM_LABEL, "synthetic": True, "topic": config.SETTING,
        "config": {"mode": args.mode, "agents": args.agents, "ticks": args.ticks,
                   "policy": args.policy, "seed": args.seed, "model": getattr(brain, "model", None)},
        "stopped": stopped_reason,
        "seconds": round(time.time() - t0, 1),
        "budget": budget.summary() if budget else None,
        "llm_errors": getattr(brain, "errors", 0),
        "llm_refusals": getattr(brain, "refusals", 0),
        "personas": [p.to_dict() for p in people],
        "metrics": history,
        "final_opinions": opinions,
        "influencers": analysis.top_influencers(args.agents, sb),
        "posts": [post_dict(p) for p in sb.posts.values()],
        "topic_table": analysis.topic_table([post_dict(p) for p in sb.visible_posts()]),
        "topic_timeline": analysis.topic_timeline([post_dict(p) for p in sb.visible_posts()]),
        "interactions": sb.interactions,
    }
    path = out_dir / "run.json"
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    print(f"Saved {path}")
    return path


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # SDK/HTTP logs can include request details; keep them quiet.
    for noisy in ("anthropic", "httpx", "httpx2", "ollama"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv=None) -> None:
    setup_logging()
    run(parse_args(argv))


if __name__ == "__main__":
    main()

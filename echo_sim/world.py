"""Persistent worlds: a platform that keeps growing across sessions.

    python -m echo_sim.world create maplebrook --residents 20
    python -m echo_sim.world run maplebrook --rounds 5 --mode ollama --policy personalized
    python -m echo_sim.world run maplebrook --rounds 5          # continues where it stopped
    python -m echo_sim.world run maplebrook --rounds 3 --add-residents 4
    python -m echo_sim.world status maplebrook
    python -m echo_sim.world list

Stop any time (Ctrl+C, or the dashboard's Pause button). Everything up to the
last finished agent turn is saved; `run` resumes from exactly there, even in
the middle of a round.
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

from . import analysis, config, engine, personas, ranking, safety, store
from .simulate import _bounded_int, setup_logging

MAX_WORLD_ROUNDS = 100_000


class _Stop(Exception):
    pass


def _name(v: str) -> str:
    try:
        return store.validate_name(v)
    except store.WorldError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Echo persistent worlds")
    p.add_argument("--root", default=str(store.WORLDS_DIR), help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="create a new world")
    c.add_argument("name", type=_name)
    c.add_argument("--residents", type=_bounded_int(2, config.MAX_AGENTS), default=20)
    c.add_argument("--seed", type=int, default=42)

    r = sub.add_parser("run", help="run more rounds (resumes automatically)")
    r.add_argument("name", type=_name)
    r.add_argument("--rounds", type=_bounded_int(1, config.MAX_TICKS), default=5)
    r.add_argument("--mode", choices=["mock", "ollama", "claude"], default="ollama")
    r.add_argument("--policy", choices=sorted(ranking.POLICIES), default="personalized")
    r.add_argument("--add-residents", type=_bounded_int(0, config.MAX_AGENTS), default=0)
    r.add_argument("--residents", type=_bounded_int(2, config.MAX_AGENTS), default=None,
                   help="create the world with this many residents if it doesn't exist yet")
    r.add_argument("--seed", type=int, default=42, help="seed used if the world is created")
    r.add_argument("--delay", type=float, default=0.0)
    r.add_argument("--max-usd", type=float, default=0.25)
    r.add_argument("--yes", action="store_true")

    s = sub.add_parser("status", help="show a world's size and progress")
    s.add_argument("name", type=_name)
    sub.add_parser("list", help="list worlds")
    args = p.parse_args(argv)
    if getattr(args, "delay", None) is not None:
        args.delay = max(0.0, min(args.delay, 5.0))
    return args


def cmd_create(args) -> None:
    path = store.create_world(args.name, args.residents, args.seed, Path(args.root))
    print(f"Created world {args.name!r} with {args.residents} residents at {path}")


def cmd_status(args) -> None:
    conn = store.connect_ro(store.db_path(args.name, Path(args.root)))
    try:
        snap = store.snapshot(conn)
    finally:
        conn.close()
    t = snap["totals"]
    print(f"World {args.name}: {snap['status']}, {snap['tick']} rounds completed, "
          f"{len(snap['agents'])} residents")
    print(f"  posts {t['posts']}  replies {t['replies']}  votes {t['votes']}  follows {t['follows']}")
    print(f"  injections quarantined {snap['counters']['quarantined']}  "
          f"rejected actions {snap['counters']['rejected']}")
    if snap["metrics"]:
        m = snap["metrics"][-1]
        print(f"  latest: policy={m['policy']} modularity={m['modularity']} "
              f"topics={m['n_topics']} top='{m['top_topic']}' homophily={m['opinion_homophily']}")


def cmd_run(args) -> None:
    root = Path(args.root)
    if not store.db_path(args.name, root).exists():
        if args.residents is None:
            sys.exit(f"World {args.name!r} doesn't exist. Create it first, or pass --residents N.")
        store.create_world(args.name, args.residents, args.seed, root)
        print(f"Created world {args.name!r} with {args.residents} residents.")

    conn = store.open_world(args.name, root)
    token = store.acquire_lease(conn)
    stop_flag = store.world_dir(args.name, root) / "STOP"
    stop_flag.unlink(missing_ok=True)
    status = "paused"
    try:
        seed = int(store.get_meta(conn, "seed", "0"))
        completed = int(store.get_meta(conn, "completed_round", "0"))
        if args.add_residents:
            n_now = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
            if n_now + args.add_residents > config.MAX_AGENTS:
                raise store.WorldError(f"a world can have at most {config.MAX_AGENTS} residents")
            taken = {r[0] for r in conn.execute("SELECT handle FROM agents")}
            newcomers = personas.generate(args.add_residents, seed, start_id=n_now, taken=taken)
            with store.tx(conn):
                store.insert_agents(conn, newcomers, joined_round=completed + 1)
            print(f"{args.add_residents} new residents joined.")

        people, opinions, memory, sb = store.load_world(conn)
        handles = {p.id: p.handle for p in people}
        end = min(completed + args.rounds, MAX_WORLD_ROUNDS)
        brain, budget = engine.make_brain(args.mode, seed + completed, max_usd=args.max_usd,
                                          yes=args.yes, est_calls=len(people) * args.rounds)
        store.set_session(conn, token, mode=args.mode, policy=args.policy,
                          target_round=end, status="running")
        print(f"World {args.name}: {completed} rounds done, {len(people)} residents, "
              f"{len(sb.posts)} posts. Running rounds {completed + 1}..{end} "
              f"({args.mode}, {args.policy} feed).")
        t0 = time.time()

        for rnd in range(completed + 1, end + 1):
            sb.start_tick()
            done = store.done_agents(conn, rnd)            # non-empty when resuming mid-round
            order = list(range(len(people)))
            random.Random(f"{seed}:order:{rnd}").shuffle(order)   # same order on resume
            for a in order:
                if stop_flag.exists():
                    raise _Stop()
                if a in done:
                    continue
                if random.Random(f"{seed}:active:{rnd}:{a}").random() > people[a].activity:
                    store.persist_turn(conn, token, sb, a, opinions[a], memory[a], None, rnd)
                    continue
                ev = engine.take_turn(brain, args.policy, sb, people[a], opinions, memory, handles, rnd, t0)
                store.persist_turn(conn, token, sb, a, opinions[a], memory[a],
                                   ev if engine.is_notable(ev) else None, rnd)
                if args.delay:
                    time.sleep(args.delay)
            m = analysis.metrics(len(people), sb, opinions)
            store.close_round(conn, token, rnd, args.policy, args.mode, m)
            spent = f" spent=${budget.spent_usd:.4f}" if budget else ""
            print(f"round {rnd:>4}: posts={m['posts']:>5} edges={m['edges']:>5} "
                  f"topics={m['n_topics']:>3} top='{m['top_topic']}' modularity={m['modularity']} homophily={m['opinion_homophily']}{spent}")
        status = "idle"
        print("Session finished. Run again any time to keep growing the world.")
    except _Stop:
        print("Paused. Run the same command to resume.")
    except safety.BudgetExceeded as e:
        print(f"Paused: {e}")
    except KeyboardInterrupt:
        print("Paused (Ctrl+C). Run the same command to resume.")
    except Exception as e:
        status = f"error: {type(e).__name__}: {str(e)[:150]}"
        raise
    finally:
        stop_flag.unlink(missing_ok=True)
        store.release_lease(conn, token, status)
        conn.close()


def main(argv=None) -> None:
    setup_logging()
    args = parse_args(argv)
    try:
        {"create": cmd_create, "run": cmd_run, "status": cmd_status,
         "list": lambda a: print("\n".join(store.list_worlds(Path(a.root))) or "(no worlds yet)")}[args.cmd](args)
    except store.WorldError as e:
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()

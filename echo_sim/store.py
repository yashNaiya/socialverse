"""Persistent world storage (SQLite).

Security design
* Parameterized queries only. No SQL is ever built from data.
* CHECK constraints enforce invariants in the database itself (stance range,
  post length, allowed action kinds), so even buggy code can't store junk.
* Every agent turn is committed in ONE transaction: stopping or crashing loses
  at most the turn in progress, and the world is never half-updated.
* Storage is not trusted on load: every stored post is re-sanitized and
  re-scanned for prompt injection before any agent can see it.
* A single-writer lease (with heartbeat) stops two sessions from writing the
  same world. Readers (the dashboard) open the file read-only.
* WAL mode lets the dashboard read while the simulation writes.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from pathlib import Path

from . import config, safety
from . import topics as topics_mod
from .personas import Persona
from .sandbox import Post, Sandbox

SCHEMA_VERSION = 2
LEASE_SECONDS = 300            # a writer silent this long is considered dead
WORLDS_DIR = Path("worlds")
_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
    id           INTEGER PRIMARY KEY CHECK (id >= 0),
    handle       TEXT NOT NULL UNIQUE CHECK (length(handle) BETWEEN 1 AND 40),
    job          TEXT NOT NULL,
    interests_json TEXT NOT NULL,
    values_json  TEXT NOT NULL,
    style        TEXT NOT NULL,
    openness     REAL NOT NULL CHECK (openness BETWEEN 0 AND 1),
    activity     REAL NOT NULL CHECK (activity BETWEEN 0 AND 1),
    opinions_json TEXT NOT NULL DEFAULT '{{}}',
    memory_json  TEXT NOT NULL DEFAULT '[]',
    joined_round INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS posts (
    id      INTEGER PRIMARY KEY CHECK (id > 0),
    author  INTEGER NOT NULL REFERENCES agents(id),
    parent  INTEGER REFERENCES posts(id),
    round   INTEGER NOT NULL,
    text    TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND {config.MAX_POST_CHARS}),
    stance  REAL NOT NULL CHECK (stance BETWEEN -1 AND 1),
    topic   TEXT NOT NULL CHECK (length(topic) BETWEEN 1 AND 40),
    agreement REAL CHECK (agreement IS NULL OR agreement BETWEEN -1 AND 1),
    score   INTEGER NOT NULL DEFAULT 0,
    replies INTEGER NOT NULL DEFAULT 0,
    visible INTEGER NOT NULL DEFAULT 1 CHECK (visible IN (0, 1))
);
CREATE TABLE IF NOT EXISTS votes (
    post_id INTEGER NOT NULL REFERENCES posts(id),
    agent   INTEGER NOT NULL REFERENCES agents(id),
    value   INTEGER NOT NULL CHECK (value IN (-1, 1)),
    round   INTEGER NOT NULL,
    PRIMARY KEY (post_id, agent)
);
CREATE TABLE IF NOT EXISTS follows (
    follower INTEGER NOT NULL REFERENCES agents(id),
    followee INTEGER NOT NULL REFERENCES agents(id),
    round    INTEGER NOT NULL,
    PRIMARY KEY (follower, followee),
    CHECK (follower <> followee)
);
CREATE TABLE IF NOT EXISTS interactions (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    src   INTEGER NOT NULL REFERENCES agents(id),
    dst   INTEGER NOT NULL REFERENCES agents(id),
    kind  TEXT NOT NULL CHECK (kind IN ('comment', 'upvote', 'downvote', 'follow')),
    round INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    round   INTEGER NOT NULL,
    agent   INTEGER NOT NULL REFERENCES agents(id),
    action  TEXT NOT NULL CHECK (action IN ('post','comment','upvote','downvote','follow','idle')),
    target  INTEGER NOT NULL,
    ok      INTEGER NOT NULL CHECK (ok IN (0, 1)),
    note    TEXT NOT NULL DEFAULT '',
    post_id INTEGER,
    stance_change REAL NOT NULL DEFAULT 0,
    topic   TEXT NOT NULL DEFAULT '' CHECK (length(topic) <= 40),
    ts      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (          -- which agents have taken their turn in a round
    round INTEGER NOT NULL,
    agent INTEGER NOT NULL REFERENCES agents(id),
    PRIMARY KEY (round, agent)
);
CREATE TABLE IF NOT EXISTS round_metrics (
    round        INTEGER PRIMARY KEY,
    policy       TEXT NOT NULL,
    mode         TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    ts           REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_parent ON posts(parent);
CREATE INDEX IF NOT EXISTS idx_events_round ON events(round);
CREATE INDEX IF NOT EXISTS idx_posts_topic ON posts(topic);
"""


class WorldError(RuntimeError):
    pass


# ---------------------------------------------------------------- paths & connections
def validate_name(name: str) -> str:
    if not _NAME_RE.fullmatch(name or ""):
        raise WorldError("world name may only contain letters, digits, - and _ (max 40)")
    return name


def world_dir(name: str, root: Path = WORLDS_DIR) -> Path:
    return Path(root) / validate_name(name)


def db_path(name: str, root: Path = WORLDS_DIR) -> Path:
    return world_dir(name, root) / "world.db"


def list_worlds(root: Path = WORLDS_DIR) -> list[str]:
    root = Path(root)
    if not root.exists():
        return []
    return sorted(d.name for d in root.iterdir()
                  if d.is_dir() and _NAME_RE.fullmatch(d.name) and (d / "world.db").exists())


def _configure(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def connect_rw(path: Path) -> sqlite3.Connection:
    conn = _configure(sqlite3.connect(path, isolation_level=None, timeout=10))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def connect_ro(path: Path) -> sqlite3.Connection:
    """Read-only connection for the dashboard: it physically cannot write."""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = _configure(sqlite3.connect(uri, uri=True, timeout=10))
    conn.execute("PRAGMA query_only = ON")
    return conn


class _Tx:
    """BEGIN IMMEDIATE ... COMMIT, rolling back on any error."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, *_):
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


def tx(conn) -> _Tx:
    return _Tx(conn)


def get_meta(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def _set_meta(conn, key: str, value) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))


# ---------------------------------------------------------------- create / lease
def create_world(name: str, residents: int, seed: int, root: Path = WORLDS_DIR) -> Path:
    from . import personas
    if not 2 <= residents <= config.MAX_AGENTS:
        raise WorldError(f"residents must be between 2 and {config.MAX_AGENTS}")
    d = world_dir(name, root)
    if (d / "world.db").exists():
        raise WorldError(f"world {name!r} already exists")
    d.mkdir(parents=True, exist_ok=True)
    conn = connect_rw(d / "world.db")
    try:
        conn.executescript(SCHEMA)
        with tx(conn):
            for k, v in {"schema_version": SCHEMA_VERSION, "name": name, "seed": seed,
                         "topic": config.SETTING, "created": time.time(), "completed_round": 0,
                         "moderated": 0, "quarantined": 0, "rejected": 0, "status": "idle",
                         "policy": "", "mode": "", "target_round": 0}.items():
                _set_meta(conn, k, v)
            insert_agents(conn, personas.generate(residents, seed), joined_round=0)
    finally:
        conn.close()
    return d / "world.db"


def insert_agents(conn, people: list[Persona], joined_round: int) -> None:
    conn.executemany(
        "INSERT INTO agents(id, handle, job, interests_json, values_json, style, openness, activity, "
        "joined_round) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(p.id, p.handle, p.job, json.dumps(p.interests), json.dumps(p.values), p.style,
          p.openness, p.activity, joined_round) for p in people])


def open_world(name: str, root: Path = WORLDS_DIR) -> sqlite3.Connection:
    path = db_path(name, root)
    if not path.exists():
        raise WorldError(f"world {name!r} does not exist")
    conn = connect_rw(path)
    version = int(get_meta(conn, "schema_version", "0"))
    if version != SCHEMA_VERSION:
        conn.close()
        raise WorldError(f"world {name!r} was created by an older version of Echo "
                         f"(schema {version}); please create a new world")
    return conn


def acquire_lease(conn) -> str:
    """Claim exclusive write access. Raises if another live session holds it."""
    token = uuid.uuid4().hex
    with tx(conn):
        holder = get_meta(conn, "writer")
        beat = float(get_meta(conn, "heartbeat", "0"))
        if holder and time.time() - beat < LEASE_SECONDS:
            raise WorldError("this world is already being run by another session "
                             f"(last active {int(time.time() - beat)}s ago)")
        _set_meta(conn, "writer", token)
        _set_meta(conn, "heartbeat", time.time())
    return token


def release_lease(conn, token: str, status: str) -> None:
    with tx(conn):
        if get_meta(conn, "writer") == token:
            conn.execute("DELETE FROM meta WHERE key = 'writer'")
        _set_meta(conn, "status", status)
        _set_meta(conn, "heartbeat", time.time())


def _check_lease(conn, token: str) -> None:
    if get_meta(conn, "writer") != token:
        raise WorldError("lost write lease; another session took over this world")
    _set_meta(conn, "heartbeat", time.time())


# ---------------------------------------------------------------- load
def load_world(conn):
    """Rebuild in-memory state from the database. Returns
    (people, opinions, memory, sandbox)."""
    rows = conn.execute("SELECT * FROM agents ORDER BY id").fetchall()
    if [r["id"] for r in rows] != list(range(len(rows))):
        raise WorldError("agent ids are not contiguous; world file may be damaged")
    people = [Persona(id=r["id"], handle=r["handle"], job=r["job"],
                      interests=[safety.sanitize(str(i), 40) for i in json.loads(r["interests_json"])][:5],
                      values=[safety.sanitize(str(v), 40) for v in json.loads(r["values_json"])][:5],
                      style=safety.sanitize(r["style"], 40),
                      openness=r["openness"], activity=r["activity"])
              for r in rows]
    opinions = []
    for r in rows:
        # Don't trust storage: topic keys must survive normalization, values are clamped.
        clean: dict[str, float] = {}
        for t, v in json.loads(r["opinions_json"]).items():
            label = topics_mod.normalize(str(t))
            if label and isinstance(v, (int, float)):
                clean[label] = max(-1.0, min(1.0, float(v)))
        opinions.append(dict(list(clean.items())[-config.MAX_OPINIONS_PER_AGENT:]))
    memory = []
    for r in rows:
        mem = [safety.sanitize(str(m), 200) for m in json.loads(r["memory_json"])]
        memory.append([m for m in mem if not safety.detect_injection(m)][-config.MAX_MEMORY_ITEMS:])

    sb = Sandbox(len(people))
    voters: dict[int, set[int]] = {}
    for v in conn.execute("SELECT post_id, agent FROM votes"):
        voters.setdefault(v["post_id"], set()).add(v["agent"])
    hidden = 0
    for r in conn.execute("SELECT * FROM posts ORDER BY id"):
        text = safety.sanitize(r["text"])
        visible = bool(r["visible"])
        # Don't trust storage: re-check content an attacker could have edited in.
        if visible and (not text or safety.detect_injection(text) or not safety.moderate(text)[0]):
            visible = False
            hidden += 1
        topic = topics_mod.normalize(r["topic"]) or topics_mod.FALLBACK
        sb.posts[r["id"]] = Post(id=r["id"], author=r["author"], text=text, stance=r["stance"],
                                 tick=r["round"], parent=r["parent"], score=r["score"],
                                 replies=r["replies"], visible=visible,
                                 voters=voters.get(r["id"], set()), topic=topic,
                                 agreement=r["agreement"])
        if visible:
            sb.topics.add_post(topic)
    sb._next_id = max(sb.posts, default=0) + 1
    sb.follows = {(r["follower"], r["followee"]) for r in conn.execute("SELECT follower, followee FROM follows")}
    sb.interactions = [(r["src"], r["dst"], r["kind"], r["round"])
                       for r in conn.execute("SELECT src, dst, kind, round FROM interactions ORDER BY id")]
    sb.moderated = int(get_meta(conn, "moderated", "0"))
    sb.quarantined = int(get_meta(conn, "quarantined", "0")) + hidden
    sb.rejected_actions = int(get_meta(conn, "rejected", "0"))
    sb.persisted_interactions = len(sb.interactions)
    return people, opinions, memory, sb


def done_agents(conn, rnd: int) -> set[int]:
    return {r["agent"] for r in conn.execute("SELECT agent FROM turns WHERE round = ?", (rnd,))}


# ---------------------------------------------------------------- write
def persist_turn(conn, token: str, sb: Sandbox, agent: int, opinions: dict[str, float],
                 memory: list[str], event: dict | None, rnd: int) -> None:
    """Write everything one agent turn changed, atomically."""
    with tx(conn):
        _check_lease(conn, token)
        pid = event["post_id"] if event else None
        if pid:
            p = sb.posts[pid]
            conn.execute("INSERT INTO posts(id, author, parent, round, text, stance, topic, agreement) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (p.id, p.author, p.parent, p.tick, p.text, p.stance, p.topic, p.agreement))
            if p.parent:
                conn.execute("UPDATE posts SET replies = replies + 1 WHERE id = ?", (p.parent,))
        if event and event["ok"] and event["action"] in ("upvote", "downvote"):
            val = 1 if event["action"] == "upvote" else -1
            conn.execute("INSERT INTO votes(post_id, agent, value, round) VALUES (?, ?, ?, ?)",
                         (event["target"], agent, val, rnd))
            conn.execute("UPDATE posts SET score = score + ? WHERE id = ?", (val, event["target"]))
        if event and event["ok"] and event["action"] == "follow":
            conn.execute("INSERT INTO follows(follower, followee, round) VALUES (?, ?, ?)",
                         (agent, event["target"], rnd))
        new = sb.interactions[sb.persisted_interactions:]
        conn.executemany("INSERT INTO interactions(src, dst, kind, round) VALUES (?, ?, ?, ?)", new)
        sb.persisted_interactions = len(sb.interactions)
        conn.execute("UPDATE agents SET opinions_json = ?, memory_json = ? WHERE id = ?",
                     (json.dumps(opinions), json.dumps(memory[-config.MAX_MEMORY_ITEMS:]), agent))
        if event:
            conn.execute("INSERT INTO events(round, agent, action, target, ok, note, post_id, stance_change, "
                         "topic, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (rnd, agent, event["action"], int(event["target"]), int(event["ok"]),
                          event["note"][:200], pid, float(event["stance_change"]),
                          event.get("topic", "")[:40], time.time()))
        conn.execute("INSERT OR IGNORE INTO turns(round, agent) VALUES (?, ?)", (rnd, agent))
        _set_meta(conn, "moderated", sb.moderated)
        _set_meta(conn, "quarantined", sb.quarantined)
        _set_meta(conn, "rejected", sb.rejected_actions)


def close_round(conn, token: str, rnd: int, policy: str, mode: str, metrics: dict) -> None:
    with tx(conn):
        _check_lease(conn, token)
        conn.execute("INSERT OR REPLACE INTO round_metrics(round, policy, mode, metrics_json, ts) "
                     "VALUES (?, ?, ?, ?, ?)", (rnd, policy, mode, json.dumps(metrics), time.time()))
        conn.execute("DELETE FROM turns WHERE round = ?", (rnd,))
        _set_meta(conn, "completed_round", rnd)


def set_session(conn, token: str, **values) -> None:
    with tx(conn):
        _check_lease(conn, token)
        for k, v in values.items():
            _set_meta(conn, k, v)


# ---------------------------------------------------------------- read (dashboard)
def _communities(n: int, edges) -> list[int]:
    import networkx as nx
    from networkx.algorithms import community
    g = nx.Graph()
    g.add_nodes_from(range(n))
    g.add_edges_from((u, v) for u, v in edges if u != v)
    label = list(range(n))
    if g.number_of_edges():
        for i, c in enumerate(community.greedy_modularity_communities(g)):
            for node in c:
                label[node] = i
    return label


def snapshot(conn) -> dict:
    """Same shape as an experiment run's live.json, so the dashboard can
    render worlds and runs with the same code."""
    meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
    agents = [dict(r) for r in conn.execute(
        "SELECT a.id, a.handle, a.job, a.opinions_json, a.joined_round, "
        " (SELECT COUNT(*) FROM follows f WHERE f.followee = a.id) AS followers, "
        " (SELECT COUNT(*) FROM posts p WHERE p.author = a.id) AS posts "
        "FROM agents a ORDER BY a.id")]
    pos_edges = [(r["src"], r["dst"]) for r in conn.execute(
        "SELECT DISTINCT src, dst FROM interactions WHERE kind IN ('comment','upvote','follow')")]
    comm = _communities(len(agents), pos_edges)
    for a in agents:
        a["opinions"] = dict(list(json.loads(a.pop("opinions_json")).items())[-5:])
        a["community"] = comm[a["id"]]
    posts = [dict(r) for r in conn.execute(
        "SELECT id, author, parent, round AS tick, score, replies, stance, text, topic, agreement FROM posts "
        "WHERE visible = 1 AND id > (SELECT COALESCE(MAX(id), 0) - 600 FROM posts) ORDER BY id")]
    events = [dict(r) for r in conn.execute(
        "SELECT round AS tick, agent, action, target, ok, note, post_id, stance_change, topic FROM events "
        "ORDER BY id DESC LIMIT 150")][::-1]
    for ev in events:
        ev["ok"] = bool(ev["ok"])
    metrics = []
    for r in conn.execute("SELECT round, policy, mode, metrics_json FROM round_metrics ORDER BY round"):
        metrics.append({"tick": r["round"], "policy": r["policy"], "mode": r["mode"],
                        **json.loads(r["metrics_json"])})
    interactions = [(r["src"], r["dst"], r["kind"], r["round"]) for r in conn.execute(
        "SELECT src, dst, kind, round FROM interactions ORDER BY id DESC LIMIT 5000")][::-1]
    totals = conn.execute(
        "SELECT (SELECT COUNT(*) FROM posts WHERE parent IS NULL) AS posts, "
        "       (SELECT COUNT(*) FROM posts WHERE parent IS NOT NULL) AS replies, "
        "       (SELECT COUNT(*) FROM votes) AS votes, "
        "       (SELECT COUNT(*) FROM follows) AS follows, "
        "       (SELECT COUNT(DISTINCT topic) FROM posts WHERE visible = 1) AS topics").fetchone()
    completed = int(meta.get("completed_round", 0))
    beat = float(meta.get("heartbeat", 0) or 0)
    running = bool(meta.get("writer")) and time.time() - beat < 120
    return {
        "label": safety.SIM_LABEL, "synthetic": True, "topic": meta.get("topic", config.SETTING),
        "kind": "world", "name": meta.get("name"),
        "status": "running" if running else meta.get("status", "idle"),
        "tick": completed,
        "config": {"mode": meta.get("mode", ""), "agents": len(agents),
                   "ticks": int(meta.get("target_round", completed) or completed),
                   "policy": meta.get("policy", ""), "seed": int(meta.get("seed", 0))},
        "updated": beat or time.time(),
        "agents": agents, "posts": posts, "events": events, "metrics": metrics,
        "counters": {"moderated": int(meta.get("moderated", 0)),
                     "quarantined": int(meta.get("quarantined", 0)),
                     "rejected": int(meta.get("rejected", 0))},
        "totals": dict(totals),
        "interactions": interactions,
    }


def export_analysis(conn) -> dict:
    """run.json-shaped export of the whole world for the analysis view."""
    import networkx as nx
    from . import analysis
    snap = snapshot(conn)
    personas_ = [dict(r) for r in conn.execute(
        "SELECT id, handle, job, interests_json, joined_round FROM agents ORDER BY id")]
    for p in personas_:
        p["interests"] = ", ".join(json.loads(p.pop("interests_json")))
    n = len(personas_)
    g = nx.Graph()
    g.add_nodes_from(range(n))
    for r in conn.execute("SELECT src, dst, COUNT(*) AS w FROM interactions "
                          "WHERE kind IN ('comment','upvote','follow') AND src <> dst GROUP BY src, dst"):
        w = g[r["src"]][r["dst"]]["weight"] + r["w"] if g.has_edge(r["src"], r["dst"]) else r["w"]
        g.add_edge(r["src"], r["dst"], weight=w)
    infl = []
    if g.number_of_edges():
        pr = nx.pagerank(g, weight="weight")
        infl = [(k, round(v, 4)) for k, v in sorted(pr.items(), key=lambda x: -x[1])[:5]]
    all_posts = [dict(r) for r in conn.execute(
        "SELECT id, author, parent, round AS tick, score, replies, stance, text, topic, agreement "
        "FROM posts WHERE visible = 1 ORDER BY id")]
    return {
        "label": snap["label"], "synthetic": True, "topic": snap["topic"],
        "config": {**snap["config"], "ticks": snap["tick"]},
        "stopped": "completed", "budget": None,
        "personas": personas_, "metrics": snap["metrics"],
        "influencers": infl, "posts": all_posts,
        "topic_table": analysis.topic_table(all_posts),
        "topic_timeline": analysis.topic_timeline(all_posts),
    }

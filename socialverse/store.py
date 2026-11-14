"""Persistent Socialverse worlds (SQLite, schema v3).

Same security design as Echo's store:
* Parameterized queries only; CHECK constraints and foreign keys enforce invariants.
* One transaction per agent turn, so a crash or pause never half-writes a turn.
* A single-writer lease: only the process that holds it may write.
* Storage is not trusted: everything is re-sanitized and re-scanned on load.
* Read-only connections (mode=ro + query_only) for listing worlds.
"""

from __future__ import annotations

import json
import os
import random
import re
import socket
import sqlite3
import time
import uuid
from pathlib import Path

from echo_sim import config, personas, safety
from echo_sim import topics as topics_mod

from . import culture

SCHEMA_VERSION = 4
LEASE_SECONDS = 300
WORLDS_DIR = Path("sv_worlds")
DB_NAME = "socialverse.db"
_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")
MAX_RESIDENTS = config.MAX_AGENTS

HAPPENINGS_SQL = """
CREATE TABLE IF NOT EXISTS happenings (      -- the fictional newsroom: news, gossip, memes, trends
    id          INTEGER PRIMARY KEY CHECK (id > 0),
    category    TEXT NOT NULL CHECK (category IN ('news','gossip','meme','trend','sports','entertainment','product','local')),
    headline    TEXT NOT NULL CHECK (length(headline) BETWEEN 1 AND 160),
    topic       TEXT NOT NULL CHECK (length(topic) BETWEEN 1 AND 40),
    sentiment   REAL NOT NULL CHECK (sentiment BETWEEN -1 AND 1),
    controversy REAL NOT NULL CHECK (controversy BETWEEN 0 AND 1),
    heat        REAL NOT NULL CHECK (heat >= 0),
    period      INTEGER NOT NULL,
    meme        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_posts_happening ON posts(happening);
"""

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agents (
    id            INTEGER PRIMARY KEY CHECK (id >= 0),
    kind          TEXT NOT NULL DEFAULT 'agent' CHECK (kind IN ('agent', 'human')),
    handle        TEXT NOT NULL UNIQUE CHECK (length(handle) BETWEEN 1 AND 40),
    job           TEXT NOT NULL,
    interests_json TEXT NOT NULL,
    values_json   TEXT NOT NULL,
    style         TEXT NOT NULL,
    openness      REAL NOT NULL CHECK (openness BETWEEN 0 AND 1),
    activity      REAL NOT NULL CHECK (activity BETWEEN 0 AND 1),
    opinions_json TEXT NOT NULL DEFAULT '{{}}',
    memory_json   TEXT NOT NULL DEFAULT '[]',
    inbox_json    TEXT NOT NULL DEFAULT '{{}}',
    valence       REAL NOT NULL DEFAULT 0 CHECK (valence BETWEEN -1 AND 1),
    arousal       REAL NOT NULL DEFAULT 0.3 CHECK (arousal BETWEEN 0 AND 1),
    next_due      REAL NOT NULL DEFAULT 0,
    joined_period INTEGER NOT NULL DEFAULT 0,
    archetype     TEXT NOT NULL DEFAULT 'local regular'
);
CREATE TABLE IF NOT EXISTS communities (
    id        INTEGER PRIMARY KEY CHECK (id > 0),
    topic     TEXT NOT NULL UNIQUE CHECK (length(topic) BETWEEN 1 AND 40),
    founder   INTEGER NOT NULL REFERENCES agents(id),
    period    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS memberships (
    community INTEGER NOT NULL REFERENCES communities(id),
    agent     INTEGER NOT NULL REFERENCES agents(id),
    period    INTEGER NOT NULL,
    PRIMARY KEY (community, agent)
);
CREATE TABLE IF NOT EXISTS posts (
    id        INTEGER PRIMARY KEY CHECK (id > 0),
    author    INTEGER NOT NULL REFERENCES agents(id),
    parent    INTEGER REFERENCES posts(id),
    community INTEGER REFERENCES communities(id),
    period    INTEGER NOT NULL,
    clock     REAL NOT NULL,
    text      TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND {config.MAX_POST_CHARS}),
    stance    REAL NOT NULL CHECK (stance BETWEEN -1 AND 1),
    topic     TEXT NOT NULL CHECK (length(topic) BETWEEN 1 AND 40),
    agreement REAL CHECK (agreement IS NULL OR agreement BETWEEN -1 AND 1),
    score     INTEGER NOT NULL DEFAULT 0,
    replies   INTEGER NOT NULL DEFAULT 0,
    visible   INTEGER NOT NULL DEFAULT 1 CHECK (visible IN (0, 1)),
    format    TEXT NOT NULL DEFAULT 'post',
    happening INTEGER REFERENCES happenings(id)
);
CREATE TABLE IF NOT EXISTS votes (
    post_id INTEGER NOT NULL REFERENCES posts(id),
    agent   INTEGER NOT NULL REFERENCES agents(id),
    value   INTEGER NOT NULL CHECK (value IN (-1, 1)),
    period  INTEGER NOT NULL,
    PRIMARY KEY (post_id, agent)
);
CREATE TABLE IF NOT EXISTS follows (
    follower INTEGER NOT NULL REFERENCES agents(id),
    followee INTEGER NOT NULL REFERENCES agents(id),
    period   INTEGER NOT NULL,
    PRIMARY KEY (follower, followee),
    CHECK (follower <> followee)
);
CREATE TABLE IF NOT EXISTS interactions (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    src    INTEGER NOT NULL REFERENCES agents(id),
    dst    INTEGER NOT NULL REFERENCES agents(id),
    kind   TEXT NOT NULL CHECK (kind IN ('comment', 'upvote', 'downvote', 'follow')),
    period INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    period   INTEGER NOT NULL,
    clock    REAL NOT NULL,
    agent    INTEGER NOT NULL REFERENCES agents(id),
    action   TEXT NOT NULL CHECK (action IN ('post','comment','upvote','downvote','follow','join','idle',
                                            'community')),
    target   INTEGER NOT NULL DEFAULT 0,
    ok       INTEGER NOT NULL CHECK (ok IN (0, 1)),
    note     TEXT NOT NULL DEFAULT '' CHECK (length(note) <= 200),
    post_id  INTEGER,
    topic    TEXT NOT NULL DEFAULT '' CHECK (length(topic) <= 40),
    stance_change REAL NOT NULL DEFAULT 0,
    emotion  TEXT NOT NULL DEFAULT '',
    human    INTEGER NOT NULL DEFAULT 0 CHECK (human IN (0, 1)),
    ts       REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS exposures (       -- "agent X was shown post Y at rank R"
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    agent   INTEGER NOT NULL REFERENCES agents(id),
    post_id INTEGER NOT NULL REFERENCES posts(id),
    period  INTEGER NOT NULL,
    rank    INTEGER NOT NULL CHECK (rank >= 0),
    policy  TEXT NOT NULL CHECK (policy IN ('chronological', 'engagement', 'personalized'))
);
CREATE TABLE IF NOT EXISTS turns (period INTEGER NOT NULL, agent INTEGER NOT NULL REFERENCES agents(id),
                                  PRIMARY KEY (period, agent));
CREATE TABLE IF NOT EXISTS period_metrics (
    period       INTEGER PRIMARY KEY,
    policy       TEXT NOT NULL,
    mode         TEXT NOT NULL,
    brain        TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    ts           REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_posts_parent ON posts(parent);
{HAPPENINGS_SQL}
CREATE INDEX IF NOT EXISTS idx_posts_topic ON posts(topic);
CREATE INDEX IF NOT EXISTS idx_posts_community ON posts(community);
CREATE INDEX IF NOT EXISTS idx_exposures_period ON exposures(period);
CREATE INDEX IF NOT EXISTS idx_events_period ON events(period);
"""


class WorldError(RuntimeError):
    pass


def validate_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise WorldError("world name may only contain letters, digits, - and _ (max 40)")
    return name


def db_path(name: str, root: Path = WORLDS_DIR) -> Path:
    return Path(root) / validate_name(name) / DB_NAME


def _configure(conn):
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def connect_rw(path: Path):
    conn = _configure(sqlite3.connect(path, isolation_level=None, timeout=10, check_same_thread=False))
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def connect_ro(path: Path):
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = _configure(sqlite3.connect(uri, uri=True, timeout=10))
    conn.execute("PRAGMA query_only = ON")
    return conn


class _Tx:
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


def set_meta(conn, key: str, value) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, str(value)))


# ---------------------------------------------------------------- create / list
def list_worlds(root: Path = WORLDS_DIR) -> list[dict]:
    root = Path(root)
    out = []
    if not root.exists():
        return out
    for d in sorted(root.iterdir()):
        if not (d.is_dir() and _NAME_RE.fullmatch(d.name) and (d / DB_NAME).exists()):
            continue
        try:
            conn = connect_ro(d / DB_NAME)
            try:
                meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
                n = conn.execute("SELECT COUNT(*) FROM agents WHERE kind = 'agent'").fetchone()[0]
                posts = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
                comms = conn.execute("SELECT COUNT(*) FROM communities").fetchone()[0]
            finally:
                conn.close()
            out.append({"name": d.name, "residents": n, "posts": posts, "communities": comms,
                        "period": int(float(meta.get("clock", 0)) // 60) + 1,
                        "created": float(meta.get("created", 0)),
                        "version": int(meta.get("schema_version", 0))})
        except sqlite3.Error:
            out.append({"name": d.name, "error": "unreadable"})
    return out


def _agent_row(p: personas.Persona, kind: str, joined: int, next_due: float):
    return (p.id, kind, p.handle, p.job, json.dumps(p.interests), json.dumps(p.values), p.style,
            p.openness, p.activity, joined, next_due)


def insert_agents(conn, people, kind: str, joined: int, clock: float, seed) -> None:
    rng = random.Random(f"{seed}:due:{joined}")
    conn.executemany(
        "INSERT INTO agents(id, kind, handle, job, interests_json, values_json, style, openness, activity, "
        "joined_period, next_due) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [_agent_row(p, kind, joined, clock + rng.uniform(0, 60)) for p in people])


def add_culture(conn, people, seed) -> None:
    """Give residents an online personality and two pop-culture interests."""
    for p in people:
        arch, pop = culture.assign(seed, p.id)
        p.interests = (list(p.interests) + pop)[:5]
        conn.execute("UPDATE agents SET archetype = ?, interests_json = ? WHERE id = ?",
                     (arch, json.dumps(p.interests), p.id))


def create_world(name: str, residents: int, seed: int, root: Path = WORLDS_DIR) -> Path:
    if not isinstance(residents, int) or not 2 <= residents <= MAX_RESIDENTS:
        raise WorldError(f"residents must be between 2 and {MAX_RESIDENTS}")
    path = db_path(name, root)
    if path.exists():
        raise WorldError(f"world {name!r} already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_rw(path)
    try:
        conn.executescript(SCHEMA)
        with tx(conn):
            for k, v in {"schema_version": SCHEMA_VERSION, "name": name, "seed": seed,
                         "setting": config.SETTING, "created": time.time(), "clock": 0.0,
                         "moderated": 0, "quarantined": 0, "rejected": 0, "status": "paused",
                         "policy": "personalized", "mode": "continuous", "brain": "mock"}.items():
                set_meta(conn, k, v)
            people = personas.generate(residents, seed)
            insert_agents(conn, people, "agent", 0, 0.0, seed)
            add_culture(conn, people, seed)
            human = personas.Persona(id=residents, handle="you", job="human visitor", interests=[],
                                     values=[], style="", openness=0.0, activity=0.0)
            insert_agents(conn, [human], "human", 0, 0.0, seed)
            set_meta(conn, "human_id", residents)
    finally:
        conn.close()
    return path


def open_world(name: str, root: Path = WORLDS_DIR):
    path = db_path(name, root)
    if not path.exists():
        raise WorldError(f"world {name!r} does not exist")
    conn = connect_rw(path)
    version = int(get_meta(conn, "schema_version", "0"))
    if version == 3:
        _migrate_v3_to_v4(conn)
        version = 4
    if version != SCHEMA_VERSION:
        conn.close()
        raise WorldError(f"world {name!r} uses schema {version}; this version needs {SCHEMA_VERSION}")
    return conn


def _migrate_v3_to_v4(conn) -> None:
    """Add online culture: archetypes, post formats, happenings."""
    statements = [s for s in HAPPENINGS_SQL.split(";") if s.strip()]
    with tx(conn):
        conn.execute(statements[0])                                   # CREATE TABLE happenings
        conn.execute("ALTER TABLE agents ADD COLUMN archetype TEXT NOT NULL DEFAULT 'local regular'")
        conn.execute("ALTER TABLE posts ADD COLUMN format TEXT NOT NULL DEFAULT 'post'")
        conn.execute("ALTER TABLE posts ADD COLUMN happening INTEGER REFERENCES happenings(id)")
        for stmt in statements[1:]:                                   # indexes
            conn.execute(stmt)
        seed = get_meta(conn, "seed", "0")
        for r in conn.execute("SELECT id, interests_json FROM agents WHERE kind = 'agent'").fetchall():
            arch, pop = culture.assign(seed, r["id"])
            interests = (json.loads(r["interests_json"]) + pop)[:5]
            conn.execute("UPDATE agents SET archetype = ?, interests_json = ? WHERE id = ?",
                         (arch, json.dumps(interests), r["id"]))
        set_meta(conn, "schema_version", 4)


# ---------------------------------------------------------------- lease
def _pid_alive(pid: int) -> bool:
    """True if a process with this id is running on this machine (never signals it)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(k32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)                                     # POSIX: signal 0 only checks existence
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_lease(conn) -> str:
    """Claim exclusive write access. A lease is honoured while its holder is alive:
    a recent heartbeat, unless the holder process on THIS machine has died
    (e.g. the server was killed), in which case it is taken over at once."""
    token = uuid.uuid4().hex
    with tx(conn):
        holder = get_meta(conn, "writer")
        beat = float(get_meta(conn, "heartbeat", "0"))
        if holder and time.time() - beat < LEASE_SECONDS:
            same_host = get_meta(conn, "writer_host") == socket.gethostname()
            pid = int(get_meta(conn, "writer_pid", "0") or 0)
            if not (same_host and pid != os.getpid() and not _pid_alive(pid)):
                raise WorldError(f"this world is open in another session (active {int(time.time() - beat)}s ago)")
        set_meta(conn, "writer", token)
        set_meta(conn, "writer_pid", os.getpid())
        set_meta(conn, "writer_host", socket.gethostname())
        set_meta(conn, "heartbeat", time.time())
    return token


def release_lease(conn, token: str) -> None:
    with tx(conn):
        if get_meta(conn, "writer") == token:
            conn.execute("DELETE FROM meta WHERE key = 'writer'")
        set_meta(conn, "status", "paused")


def check_lease(conn, token: str) -> None:
    if get_meta(conn, "writer") != token:
        raise WorldError("lost write lease; another session took over this world")
    set_meta(conn, "heartbeat", time.time())


# ---------------------------------------------------------------- load (storage is untrusted)
def _clean_list(raw: str, n: int, chars: int = 40) -> list[str]:
    try:
        vals = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    return [safety.sanitize(str(v), chars) for v in vals if isinstance(v, (str, int, float))][:n]


def _clean_opinions(raw: str) -> dict[str, float]:
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    clean: dict[str, float] = {}
    if isinstance(data, dict):
        for t, v in data.items():
            label = topics_mod.normalize(str(t))
            if label and isinstance(v, (int, float)):
                clean[label] = max(-1.0, min(1.0, float(v)))
    return dict(list(clean.items())[-config.MAX_OPINIONS_PER_AGENT:])


def _clean_inbox(raw: str) -> dict[str, int]:
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    keys = ("up", "down", "agree", "disagree", "followers", "replies")
    return {k: max(0, min(1000, int(data[k]))) for k in keys
            if isinstance(data, dict) and isinstance(data.get(k), (int, float))}


def load_rows(conn) -> dict:
    """Read and re-validate everything. Returns plain data for World to assemble."""
    agents = []
    rows = conn.execute("SELECT * FROM agents ORDER BY id").fetchall()
    if [r["id"] for r in rows] != list(range(len(rows))):
        raise WorldError("agent ids are not contiguous; world file may be damaged")
    for r in rows:
        mem = [m for m in _clean_list(r["memory_json"], 50, 200) if not safety.detect_injection(m)]
        agents.append({
            "persona": personas.Persona(
                id=r["id"], handle=safety.sanitize(r["handle"], 40) or f"resident{r['id']}",
                job=safety.sanitize(r["job"], 40), interests=_clean_list(r["interests_json"], 5),
                values=_clean_list(r["values_json"], 5), style=safety.sanitize(r["style"], 40),
                openness=r["openness"], activity=r["activity"]),
            "kind": r["kind"], "opinions": _clean_opinions(r["opinions_json"]),
            "memory": mem[-config.MAX_MEMORY_ITEMS:], "inbox": _clean_inbox(r["inbox_json"]),
            "valence": r["valence"], "arousal": r["arousal"], "next_due": r["next_due"],
            "joined": r["joined_period"],
            "archetype": r["archetype"] if r["archetype"] in culture.ARCHETYPES else "local regular",
        })
    communities = []
    for r in conn.execute("SELECT * FROM communities ORDER BY id"):
        label = topics_mod.normalize(r["topic"])
        if label:
            communities.append({"id": r["id"], "topic": label, "founder": r["founder"], "period": r["period"]})
    members = [(r["community"], r["agent"]) for r in conn.execute("SELECT community, agent FROM memberships")]
    voters: dict[int, set[int]] = {}
    for v in conn.execute("SELECT post_id, agent FROM votes"):
        voters.setdefault(v["post_id"], set()).add(v["agent"])
    posts, hidden = [], 0
    for r in conn.execute("SELECT * FROM posts ORDER BY id"):
        text = safety.sanitize(r["text"])
        visible = bool(r["visible"])
        if visible and (not text or safety.detect_injection(text) or not safety.moderate(text)[0]):
            visible, hidden = False, hidden + 1
        fmt = r["format"] if r["format"] in culture.FORMATS else "post"
        posts.append({**dict(r), "text": text, "visible": visible, "format": fmt,
                      "topic": topics_mod.normalize(r["topic"]) or topics_mod.FALLBACK,
                      "voters": voters.get(r["id"], set())})
    return {
        "agents": agents, "communities": communities, "members": members, "posts": posts,
        "follows": {(r["follower"], r["followee"]) for r in conn.execute("SELECT follower, followee FROM follows")},
        "interactions": [(r["src"], r["dst"], r["kind"], r["period"])
                         for r in conn.execute("SELECT src, dst, kind, period FROM interactions ORDER BY id")],
        "turns": {(r["period"], r["agent"]) for r in conn.execute("SELECT period, agent FROM turns")},
        "meta": {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")},
        "hidden": hidden,
        "happenings": [culture.Happening(id=r["id"], category=r["category"],
                                         headline=safety.sanitize(r["headline"], 160),
                                         topic=topics_mod.normalize(r["topic"]) or topics_mod.FALLBACK,
                                         sentiment=r["sentiment"], controversy=r["controversy"], heat=r["heat"],
                                         period=r["period"], extra={"meme": r["meme"]} if r["meme"] else {})
                       for r in conn.execute("SELECT * FROM happenings ORDER BY id")
                       if r["category"] in culture.CATEGORIES],
    }

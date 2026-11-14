"""Echo dashboard: a live, social-media-style view of the simulation plus
an analysis view for finished runs.

    streamlit run dashboard.py

Security notes
* Binds to localhost only (.streamlit/config.toml).
* Every piece of agent-written text is HTML-escaped before rendering, so a
  post can never inject markup or script into the page.
* Simulations are launched as a subprocess with a fixed argument list (no
  shell), only in the free modes, one at a time, with bounded parameters.
* Runs are chosen from a fixed list; paths are checked to stay inside ./runs.
"""

from __future__ import annotations

import html
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import networkx as nx
import pandas as pd
import streamlit as st

from echo_sim import store

ROOT = Path(__file__).parent.resolve()
RUNS = ROOT / "runs"
LOGS = RUNS / "_logs"
WORLDS = ROOT / "worlds"
POLICIES = ["personalized", "engagement", "chronological"]
FOR, AGAINST, NEUTRAL = "#2b83ba", "#d7191c", "#8c8c8c"

st.set_page_config(page_title="Echo", page_icon="🗣️", layout="wide")

st.markdown("""
<style>
.echo-card{border:1px solid rgba(127,127,127,.25);border-radius:12px;padding:12px 14px;margin:0 0 10px;
           background:rgba(127,127,127,.05)}
.echo-new{border-color:#f0a202;box-shadow:0 0 0 2px rgba(240,162,2,.25)}
.echo-head{display:flex;align-items:center;gap:8px;font-size:.85rem;opacity:.9}
.echo-av{width:30px;height:30px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;
         color:#fff;font-weight:700;font-size:.75rem;flex:none}
.echo-handle{font-weight:700}
.echo-muted{opacity:.6}
.echo-body{margin:8px 0 6px;font-size:.98rem;line-height:1.4}
.echo-foot{display:flex;gap:14px;font-size:.8rem;opacity:.8;align-items:center}
.echo-chip{border-radius:999px;padding:1px 8px;font-size:.72rem;color:#fff}
.echo-badge{background:#f0a202;color:#000;border-radius:6px;padding:0 6px;font-size:.7rem;font-weight:700}
.echo-reply{border-left:3px solid rgba(127,127,127,.35);margin:6px 0 0 18px;padding:4px 0 2px 10px}
.echo-ev{font-size:.82rem;padding:5px 0;border-bottom:1px solid rgba(127,127,127,.15)}
.echo-ev-bad{color:#d7191c}
.echo-bar{height:10px;border-radius:5px;display:inline-block}
.echo-topic{border-radius:6px;padding:1px 7px;font-size:.74rem;font-weight:600;
            background:rgba(127,127,127,.18)}
.echo-pill{border-radius:999px;padding:2px 10px;font-size:.8rem;font-weight:700;color:#fff}
</style>
""", unsafe_allow_html=True)

e = html.escape   # every agent-supplied string goes through this


# ---------------------------------------------------------------- helpers
def stance_color(s: float) -> str:
    return FOR if s > 0.2 else AGAINST if s < -0.2 else NEUTRAL


PALETTE = ["#4e79a7", "#f28e2b", "#59a14f", "#e15759", "#76b7b2", "#edc948", "#b07aa1",
           "#ff9da7", "#9c755f", "#bab0ac"]


def handle_color(handle: str) -> str:
    return PALETTE[sum(map(ord, handle)) % len(PALETTE)]


def stance_label(s: float) -> str:
    return "positive" if s > 0.2 else "critical" if s < -0.2 else "neutral"


def list_runs() -> list[str]:
    if not RUNS.exists():
        return []
    dirs = [d for d in RUNS.iterdir() if d.is_dir() and not d.name.startswith("_")
            and ((d / "live.json").exists() or (d / "run.json").exists())]
    return [d.name for d in sorted(dirs, key=lambda d: d.stat().st_mtime, reverse=True)]


def safe_run_dir(name: str) -> Path | None:
    """Resolve a run folder, refusing anything outside ./runs. The folder may
    not exist yet for a run that was just launched."""
    p = (RUNS / name).resolve()
    return p if p.parent == RUNS.resolve() else None


def load_json(path: Path) -> dict | None:
    for _ in range(5):                  # the simulator may be mid-rename
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, PermissionError):
            time.sleep(0.05)
        except FileNotFoundError:
            return None
    return None


def active_runs() -> list[str]:
    """Runs whose live file says running and was updated recently."""
    out = []
    for name in list_runs():
        d = load_json(RUNS / name / "live.json")
        if d and d.get("status") == "running" and time.time() - d.get("updated", 0) < 180:
            out.append(name)
    return out


def avatar(handle: str) -> str:
    initials = "".join(w[0] for w in handle.split("_")[:2]).upper()
    return f'<span class="echo-av" style="background:{handle_color(handle)}">{e(initials)}</span>'


def launch(argv: list[str], log_name: str) -> None:
    """Start a simulator subprocess: fixed argv, no shell, output to a log file."""
    LOGS.mkdir(parents=True, exist_ok=True)
    log = open(LOGS / log_name, "w", encoding="utf-8")
    subprocess.Popen([sys.executable, "-u", "-m", *argv], cwd=ROOT, stdout=log,
                     stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, shell=False)


def world_snapshot(name: str) -> dict | None:
    path = store.db_path(name, WORLDS)
    if not path.exists():
        return None
    try:
        conn = store.connect_ro(path)
    except sqlite3.Error:
        return None
    try:
        version = int(store.get_meta(conn, "schema_version", "0") or 0)
        if version != store.SCHEMA_VERSION:
            st.error(f"World '{name}' was created by an older version of Echo. "
                     "Please create a new world.")
            st.stop()
        return store.snapshot(conn)
    except sqlite3.Error:
        return None                     # e.g. schema still being created
    finally:
        conn.close()


def world_analysis(name: str) -> dict | None:
    path = store.db_path(name, WORLDS)
    if not path.exists():
        return None
    conn = store.connect_ro(path)
    try:
        return store.export_analysis(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------- sidebar
st.sidebar.title("🗣️ Echo")
source = st.sidebar.radio("Platform", ["🌍 Worlds", "🧪 Experiments"], horizontal=True,
                          help="Worlds keep growing across sessions. Experiments start fresh every time, "
                               "for fair comparisons between feed algorithms.")
view = st.sidebar.radio("View", ["Live feed", "Analysis"], horizontal=True)
MODE_LABELS = {"ollama": "Local AI (Ollama, free)", "mock": "Rule-based (instant, free)"}

if source == "🌍 Worlds":
    NEW = "➕ Create a new world"
    worlds = store.list_worlds(WORLDS)
    goto = st.session_state.pop("_goto_world", None)
    if goto:
        st.session_state["world_pending"] = goto
    pending_w = st.session_state.get("world_pending")
    if pending_w and pending_w not in worlds and (LOGS / f"world_{pending_w}.log").exists():
        worlds.insert(0, pending_w)                    # being created right now
    options = worlds + [NEW]
    if goto:
        st.session_state["world_pick"] = goto
    if st.session_state.get("world_pick") not in options:
        st.session_state["world_pick"] = options[0]
    wname = st.sidebar.selectbox("World", options, key="world_pick",
                                 on_change=lambda: st.session_state.update(seen=set()))
    is_new = wname == NEW
    snap_now = None if is_new else world_snapshot(wname)
    running_now = bool(snap_now and snap_now["status"] == "running")

    with st.sidebar.expander("▶ Run controls", expanded=True):
        if is_new:
            new_name = st.text_input("World name", "maplebrook", max_chars=40)
            residents = st.slider("Starting residents", 4, 40, 15)
            seed = st.number_input("Seed", 0, 10_000, 42)
        else:
            st.caption(f"{snap_now['tick'] if snap_now else 0} rounds so far · "
                       f"{len(snap_now['agents']) if snap_now else '?'} residents")
        mode = st.selectbox("Agents", ["ollama", "mock"], format_func=MODE_LABELS.get, key="w_mode")
        policy = st.selectbox("Feed algorithm", POLICIES, key="w_policy",
                              help="You can change this between sessions to see how the platform reacts.")
        rounds = st.slider("Rounds to run now", 1, 20, 3)
        add_res = 0 if is_new else st.slider("New residents joining", 0, 10, 0)
        delay = st.slider("Pause per action (s)", 0.0, 1.0, 0.0 if mode == "ollama" else 0.25, 0.05, key="w_delay")
        label = "Create & run" if is_new else f"▶ Run {rounds} more rounds"
        if st.button(label, type="primary", width="stretch", disabled=running_now):
            target = new_name.strip() if is_new else wname
            try:
                store.validate_name(target)
                if is_new and target in store.list_worlds(WORLDS):
                    raise store.WorldError(f"a world called {target!r} already exists")
                argv = ["echo_sim.world", "run", target, "--rounds", str(int(rounds)),
                        "--mode", mode, "--policy", policy, "--delay", f"{float(delay):.2f}"]
                if is_new:
                    argv += ["--residents", str(int(residents)), "--seed", str(int(seed))]
                if add_res:
                    argv += ["--add-residents", str(int(add_res))]
                launch(argv, f"world_{target}.log")
                st.session_state["_goto_world"] = target
                st.session_state["seen"] = set()
                time.sleep(1.0)
                st.rerun()
            except store.WorldError as err:
                st.error(str(err))
        if running_now:
            st.caption("Running… press Pause to stop. It resumes exactly where it left off.")

    if not is_new and st.sidebar.button("⏸ Pause", width="stretch", disabled=not running_now):
        (store.world_dir(wname, WORLDS) / "STOP").touch()
        st.sidebar.info("Pausing after the current action…")

    if is_new:
        st.title("Create a world")
        st.markdown("A **world** is a persistent platform. Every post, vote, follow and opinion is saved "
                    "to its own database, so you can pause at any time and keep growing it later. "
                    "Set it up in the sidebar and press **Create & run**.")
        st.stop()
    source_key = f"world_{wname}"
    log_file = LOGS / f"world_{wname}.log"
    load_live = lambda: world_snapshot(wname)            # noqa: E731
    load_analysis = lambda: world_analysis(wname)        # noqa: E731
    has_live = True

else:
    with st.sidebar.expander("▶ Start a new experiment run", expanded=not list_runs()):
        mode = st.selectbox("Agents", ["ollama", "mock"], format_func=MODE_LABELS.get)
        n_agents = st.slider("Residents", 4, 40, 12)
        n_ticks = st.slider("Rounds", 1, 20, 6)
        policy = st.selectbox("Feed algorithm", POLICIES)
        seed = st.number_input("Seed", 0, 10_000, 42)
        delay = st.slider("Pause per action (s)", 0.0, 1.0, 0.0 if mode == "ollama" else 0.25, 0.05)
        if st.button("Start", type="primary", width="stretch"):
            busy = active_runs()
            if busy:
                st.error(f"A simulation is already running ({busy[0]}). Stop it first.")
            else:
                run_id = f"live_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{mode}_{policy}"
                # Fixed argv, no shell, values come from bounded widgets only.
                launch(["echo_sim.simulate", "--mode", mode, "--agents", str(int(n_agents)),
                        "--ticks", str(int(n_ticks)), "--policy", policy, "--seed", str(int(seed)),
                        "--delay", f"{float(delay):.2f}", "--run-id", run_id], f"{run_id}.log")
                st.session_state["run"] = run_id
                st.session_state["run_pick"] = run_id
                st.session_state["seen"] = set()
                time.sleep(1.0)
                st.rerun()

    runs = list_runs()
    pending = st.session_state.get("run")
    if pending and pending not in runs and (LOGS / f"{pending}.log").exists():
        runs.insert(0, pending)          # just launched; its folder appears in a moment
    if not runs:
        st.title("Experiment runs")
        st.info("No runs yet. Open **Start a new experiment run** in the sidebar.")
        st.stop()
    if st.session_state.get("run_pick") not in runs:
        st.session_state["run_pick"] = pending if pending in runs else runs[0]
    choice = st.sidebar.selectbox("Run", runs, key="run_pick",
                                  on_change=lambda: st.session_state.update(seen=set()))
    st.session_state["run"] = choice
    run_dir = safe_run_dir(choice)
    if run_dir is None:
        st.error("Invalid run")
        st.stop()
    if st.sidebar.button("■ Stop this run", width="stretch"):
        if run_dir.is_dir():
            (run_dir / "STOP").touch()
            st.sidebar.info("Stop requested. The simulation finishes its current action.")
    source_key = f"run_{choice}"
    log_file = LOGS / f"{choice}.log"
    load_live = lambda: load_json(run_dir / "live.json")      # noqa: E731
    load_analysis = lambda: load_json(run_dir / "run.json")   # noqa: E731
    has_live = (run_dir / "live.json").exists() or log_file.exists()


# ---------------------------------------------------------------- live view
def render_post(p: dict, agents: list[dict], replies: list[dict], seen: set) -> str:
    a = agents[p["author"]]
    new = p["id"] not in seen
    badge = ' <span class="echo-badge">NEW</span>' if new else ""
    topic = p.get("topic", "general chat")
    chip = (f'<span class="echo-topic">#{e(topic)}</span>'
            f'<span class="echo-chip" style="background:{stance_color(p["stance"])}">'
            f'{stance_label(p["stance"])}</span>')
    out = [f'<div class="echo-card{" echo-new" if new else ""}">',
           f'<div class="echo-head">{avatar(a["handle"])}'
           f'<span class="echo-handle">u/{e(a["handle"])}</span>'
           f'<span class="echo-muted">· {e(a["job"])} · round {int(p["tick"])} · #{int(p["id"])}</span>{badge}</div>',
           f'<div class="echo-body">{e(p["text"])}</div>',
           f'<div class="echo-foot"><span>▲ <b>{int(p["score"])}</b> ▼</span>'
           f'<span>💬 {int(p["replies"])}</span>{chip}</div>']
    for r in replies[-3:]:
        ra = agents[r["author"]]
        rnew = ' <span class="echo-badge">NEW</span>' if r["id"] not in seen else ""
        agr = r.get("agreement")
        achip = ("" if agr is None else
                 f' <span class="echo-chip" style="background:{FOR if agr > 0.2 else AGAINST if agr < -0.2 else NEUTRAL}">'
                 f'{"agrees" if agr > 0.2 else "disagrees" if agr < -0.2 else "neutral"}</span>')
        out.append(f'<div class="echo-reply"><div class="echo-head">{avatar(ra["handle"])}'
                   f'<span class="echo-handle">u/{e(ra["handle"])}</span>'
                   f'<span class="echo-muted">· ▲ {int(r["score"])}</span>{achip}{rnew}</div>'
                   f'<div class="echo-body">{e(r["text"])}</div></div>')
    if len(replies) > 3:
        out.append(f'<div class="echo-reply echo-muted">+ {len(replies) - 3} earlier replies</div>')
    out.append("</div>")
    return "".join(out)


ICONS = {"post": "📝", "comment": "💬", "upvote": "⬆️", "downvote": "⬇️", "follow": "➕", "idle": "💭"}


def render_event(ev: dict, agents: list[dict]) -> str:
    who = f'<b>u/{e(agents[ev["agent"]]["handle"])}</b>'
    act, tgt = ev["action"], int(ev["target"])
    if act == "post":
        what = (f"posted #{ev['post_id']} about <b>#{e(ev.get('topic', ''))}</b>"
                if ev["post_id"] else "tried to post")
    elif act == "comment":
        what = f"replied to #{tgt}"
    elif act in ("upvote", "downvote"):
        what = f"{act}d #{tgt}"
    elif act == "follow":
        what = f"followed u/{e(agents[tgt]['handle'])}" if 0 <= tgt < len(agents) else "tried to follow"
    else:
        what = "changed their mind"
    shift = ev.get("stance_change", 0)
    if abs(shift) > 0.05:
        what += (f' <span class="echo-muted">(opinion on #{e(ev.get("topic", ""))} '
                 f'{"↑" if shift > 0 else "↓"} {shift:+.2f})</span>')
    bad = "" if ev["ok"] else f' <span class="echo-ev-bad">⛔ {e(ev["note"])}</span>'
    return f'<div class="echo-ev">{ICONS.get(act, "•")} {who} {what}{bad} <span class="echo-muted">· r{ev["tick"]}</span></div>'


def network_figure(data: dict):
    n = data["config"]["agents"]
    g = nx.Graph()
    g.add_nodes_from(range(n))
    for src, dst, kind, _ in data["interactions"]:
        if kind in {"upvote", "comment", "follow"} and src != dst:
            g.add_edge(src, dst, weight=g[src][dst]["weight"] + 1 if g.has_edge(src, dst) else 1)
    key = f"pos_{source_key}"
    prev = st.session_state.get(key)
    pos = nx.spring_layout(g, pos=prev, seed=1, iterations=30 if prev is None else 8, k=1.2 / max(n, 1) ** 0.5)
    st.session_state[key] = pos
    fig, ax = plt.subplots(figsize=(4.6, 3.8))
    fig.patch.set_alpha(0)
    ax.set_facecolor("none")
    nx.draw_networkx_edges(g, pos, ax=ax, alpha=0.35, width=0.8, edge_color="#888888")
    nx.draw_networkx_nodes(g, pos, ax=ax,
                           node_color=[PALETTE[a.get("community", 0) % len(PALETTE)] for a in data["agents"]],
                           node_size=[60 + 40 * a["followers"] for a in data["agents"]],
                           edgecolors="white", linewidths=0.6)
    ax.axis("off")
    fig.tight_layout(pad=0.2)
    return fig


@st.fragment(run_every=2)
def live_view():
    data = load_live()
    if data is None:
        st.info("Starting up… (loading the model can take a few seconds)")
        if log_file.exists():
            tail = log_file.read_text(encoding="utf-8", errors="replace")[-1500:]
            if "Error" in tail or "Traceback" in tail:
                st.error("The simulation failed to start:")
                st.code(tail)
        return
    agents, posts, cfg = data["agents"], data["posts"], data["config"]
    seen: set = st.session_state.setdefault("seen", set())
    status = data["status"]
    running = status == "running" and time.time() - data["updated"] < 180

    # ---- header
    is_world = data.get("kind") == "world"
    if is_world:
        running = status == "running"
    pill_color = ("#2ca02c" if running else "#555" if status in ("completed", "idle")
                  else "#f0a202" if status == "paused" else "#d7191c")
    pill_text = "● LIVE" if running else status.upper()[:40]
    title = f"🌍 {data['name']}" if is_world else "Maplebrook Town Square"
    st.markdown(f'<h2 style="margin-bottom:0">{e(title)} '
                f'<span class="echo-pill" style="background:{pill_color}">{e(pill_text)}</span></h2>'
                f'<div class="echo-muted">{e(data["topic"])} · feed: <b>{e(cfg["policy"] or "-")}</b> · '
                f'agents: {e(cfg["mode"] or "-")} · ⚠️ all content is AI-generated and synthetic</div>',
                unsafe_allow_html=True)
    if is_world:
        target = f" · this session runs to round {cfg['ticks']}" if running else ""
        st.caption(f"**{data['tick']} rounds completed** · {len(agents)} residents{target}")
    else:
        st.progress(min(1.0, data["tick"] / max(1, cfg["ticks"])),
                    text=f"Round {data['tick']} of {cfg['ticks']}")

    kinds = [k for _, _, k, _ in data["interactions"]]
    c = data["counters"]
    t = data.get("totals") or {
        "posts": sum(1 for p in posts if not p["parent"]),
        "replies": sum(1 for p in posts if p["parent"]),
        "votes": kinds.count("upvote") + kinds.count("downvote"),
        "follows": kinds.count("follow")}
    m = st.columns(6)
    m[0].metric("Posts", t["posts"])
    m[1].metric("Replies", t["replies"])
    m[2].metric("Votes", t["votes"])
    m[3].metric("Follows", t["follows"])
    m[4].metric("Injections blocked", c["quarantined"])
    m[5].metric("Actions rejected", c["rejected"])

    feed_col, side_col = st.columns([1.7, 1])

    # ---- feed
    with feed_col:
        sort = st.radio("Sort", ["New", "Hot", "Top"], horizontal=True, label_visibility="collapsed",
                        key="sort")
        topic_counts = pd.Series([p.get("topic", "general chat") for p in posts]).value_counts()
        pick = st.selectbox("Topic", ["All topics"] + list(topic_counts.index[:30]),
                            label_visibility="collapsed", key="topic_filter")
        tops = [p for p in posts if not p["parent"]
                and (pick == "All topics" or p.get("topic", "general chat") == pick)]
        by_parent: dict[int, list] = {}
        for p in posts:
            if p["parent"]:
                by_parent.setdefault(p["parent"], []).append(p)
        now = data["tick"]
        if sort == "New":
            tops.sort(key=lambda p: max([p["id"]] + [r["id"] for r in by_parent.get(p["id"], [])]), reverse=True)
        elif sort == "Hot":
            tops.sort(key=lambda p: (p["score"] + 2 * p["replies"] + 1) / (1 + now - p["tick"]) ** 1.5, reverse=True)
        else:
            tops.sort(key=lambda p: (p["score"], p["replies"]), reverse=True)
        if not tops:
            st.caption("No posts yet. Residents are reading the news…")
        st.markdown("".join(render_post(p, agents, by_parent.get(p["id"], []), seen) for p in tops[:25]),
                    unsafe_allow_html=True)

    # ---- sidebar column
    with side_col:
        st.markdown("#### ⚡ Live activity")
        evs = list(reversed(data["events"]))[:18]
        st.markdown("".join(render_event(ev, agents) for ev in evs) or
                    '<span class="echo-muted">Waiting for the first move…</span>', unsafe_allow_html=True)

        st.markdown("#### 🔥 Trending topics")
        recent = [p for p in posts if p["tick"] >= max(1, data["tick"] - 2)] or posts
        trend: dict[str, list] = {}
        for p in recent:
            trend.setdefault(p.get("topic", "general chat"), []).append(p["stance"])
        rows = []
        for t, vals in sorted(trend.items(), key=lambda kv: -len(kv[1]))[:6]:
            pos_ = sum(v > 0.2 for v in vals)
            neg_ = sum(v < -0.2 for v in vals)
            neu_ = len(vals) - pos_ - neg_
            bars = "".join(f'<span class="echo-bar" style="width:{100 * k / len(vals):.1f}%;background:{col}"></span>'
                           for k, col in ((neg_, AGAINST), (neu_, NEUTRAL), (pos_, FOR)) if k)
            rows.append(f'<div class="echo-ev"><b>#{e(t)}</b> <span class="echo-muted">· {len(vals)} posts</span>'
                        f'<div style="display:flex;width:100%;margin-top:3px">{bars}</div></div>')
        st.markdown("".join(rows) or '<span class="echo-muted">No topics yet.</span>', unsafe_allow_html=True)
        st.caption("Bar = critical · neutral · positive posts in the last 3 rounds.")

        st.markdown("#### 🕸️ Who talks to whom")
        fig = network_figure(data)
        st.pyplot(fig, clear_figure=True)
        plt.close(fig)
        st.caption("Colour = detected community, size = followers. Tight single-colour clusters "
                   "that rarely connect = echo chambers.")

        st.markdown("#### 🏆 Top voices")
        top = sorted(agents, key=lambda a: (a["followers"], a["posts"]), reverse=True)[:5]
        st.markdown("".join(
            f'<div class="echo-ev">{avatar(a["handle"])} <b>u/{e(a["handle"])}</b> '
            f'<span class="echo-muted">· {a["followers"]} followers · {a["posts"]} posts</span></div>'
            for a in top), unsafe_allow_html=True)

        if len(data["metrics"]) > 1:
            st.markdown("#### 📈 Echo-chamber signals")
            mdf = pd.DataFrame(data["metrics"]).set_index("tick")
            cols = [c for c in ("modularity", "opinion_homophily", "cross_cutting_ratio") if c in mdf]
            if cols:
                st.line_chart(mdf[cols], height=180)

    seen.update(p["id"] for p in posts)


# ---------------------------------------------------------------- analysis view
def analysis_view():
    data = load_analysis()
    if data is None:
        st.info("This run hasn't finished yet. Analysis appears when it completes. Use **Live feed** to watch it.")
        return
    if not data["metrics"]:
        st.info("No completed rounds yet. Run the world for at least one round.")
        return
    cfg = data["config"]
    st.title("World analysis" if source == "🌍 Worlds" else "Run analysis")
    if "policy" in data["metrics"][0]:
        # Show when the feed algorithm changed during the world's life.
        spans, cur = [], None
        for row in data["metrics"]:
            if cur is None or row["policy"] != cur["policy"] or row["mode"] != cur["mode"]:
                cur = {"policy": row["policy"], "mode": row["mode"], "from": row["tick"], "to": row["tick"]}
                spans.append(cur)
            cur["to"] = row["tick"]
        st.caption("Feed algorithm history: " + " → ".join(
            f"rounds {s['from']}–{s['to']}: {s['policy']} ({s['mode']})" for s in spans))
    st.caption(f"{data['topic']} · ⚠️ synthetic data")
    c = st.columns(5)
    c[0].metric("Mode", cfg["mode"])
    c[1].metric("Policy", cfg["policy"])
    c[2].metric("Agents", cfg["agents"])
    c[3].metric("Rounds", cfg["ticks"])
    c[4].metric("Spent", f"${data['budget']['spent_usd']:.4f}" if data["budget"] else "$0 (free)")
    if data["stopped"] != "completed":
        st.warning(f"Run ended early: {data['stopped']}")
    if "topic_table" not in data:
        st.info("This run was recorded by an older version of Echo (single fixed topic). "
                "Start a new run or world to see emergent-topic analysis.")
        return
    m = pd.DataFrame(data["metrics"]).set_index("tick")
    handles = {p["id"]: p["handle"] for p in data["personas"]}
    st.subheader("What residents chose to talk about")
    tl = pd.DataFrame(data["topic_timeline"]).fillna(0)
    if not tl.empty:
        st.area_chart(tl.set_index("round"))
        st.caption("Posts + replies per round for the most active topics. Nobody assigned these topics; "
                   "agents picked them.")
    st.dataframe(pd.DataFrame(data["topic_table"]), hide_index=True, width="stretch",
                 column_config={"mean_opinion": st.column_config.NumberColumn(format="%.2f"),
                                "polarization": st.column_config.NumberColumn(
                                    help="Spread of opinions within the topic (higher = more divided)")})
    left, right = st.columns(2)
    with left:
        st.subheader("Topics & opinions")
        st.line_chart(m[[c for c in ("n_topics", "topic_concentration") if c in m]])
        st.line_chart(m[[c for c in ("polarization", "extremity", "disagreement_rate") if c in m]])
    with right:
        st.subheader("Network structure")
        st.line_chart(m[[c for c in ("modularity", "opinion_homophily", "cross_cutting_ratio") if c in m]])
        st.subheader("Top influencers (PageRank)")
        st.dataframe(pd.DataFrame([{"handle": handles[a], "pagerank": s} for a, s in data["influencers"]]),
                     hide_index=True)
    with st.expander("What do these metrics mean?"):
        st.markdown(
            "- **n_topics / topic_concentration**: how many topics exist, and how much attention "
            "piles onto a few of them (1 = everyone on one topic).\n"
            "- **polarization**: how divided opinions are within each topic.\n"
            "- **opinion_homophily**: how much more connected residents agree than random pairs "
            "(> 0 means echo chamber).\n"
            "- **cross_cutting_ratio**: share of connections between people who disagree on a shared topic.\n"
            "- **disagreement_rate**: share of replies that disagree with the post they answer.\n"
            "- **modularity**: how strongly the network splits into separate communities.")
    st.subheader("All posts")
    posts = pd.DataFrame(data["posts"])
    if not posts.empty:
        posts["author"] = posts["author"].map(handles)
        st.dataframe(posts[["id", "tick", "topic", "author", "parent", "score", "stance", "agreement", "text"]],
                     hide_index=True, width="stretch")


if view == "Live feed":
    if has_live:
        live_view()
    else:
        st.info("This older run has no live recording. Use **Analysis**, or start a new simulation.")
else:
    analysis_view()

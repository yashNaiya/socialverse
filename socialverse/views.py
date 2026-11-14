"""Read models for the API. Every function must be called with world.lock held.
All agent text is already sanitized; the React UI renders it as plain text."""

from __future__ import annotations

from collections import Counter, defaultdict

import networkx as nx
from networkx.algorithms import community as nx_comm

from echo_sim import analysis, safety

from . import culture, emotions
from .feed import _engagement


def _counts(w):
    posts = Counter(p.author for p in w.sb.posts.values() if p.visible)
    followers = Counter(b for (_, b) in w.sb.follows)
    following = Counter(a for (a, _) in w.sb.follows)
    return posts, followers, following


def summary(w) -> dict:
    posts_by, followers, following = _counts(w)
    visible = [p for p in w.sb.posts.values() if p.visible]
    comm_posts = Counter(p.community for p in visible if p.community is not None)
    comms = []
    for cid, c in sorted(w.communities.items()):
        mem = w.members[cid]
        agents_ = [a for a in mem if w.kinds[a] == "agent"]
        ops = [w.opinions[a][c["topic"]] for a in agents_ if c["topic"] in w.opinions[a]]
        comms.append({
            "id": cid, "name": f"c/{c['topic']}", "topic": c["topic"], "founder": c["founder"],
            "founder_handle": w.handles.get(c["founder"]), "founded_period": c["period"],
            "members": len(mem), "posts": comm_posts.get(cid, 0),
            "mood": round(sum(w.emotions[a].valence for a in agents_) / len(agents_), 3) if agents_ else 0,
            "mean_opinion": round(sum(ops) / len(ops), 3) if ops else None,
            "joined": cid in w.agent_comms[w.human_id] if w.human_id >= 0 else False,
        })
    agents = []
    for p in w.people:
        a = p.id
        agents.append({
            "id": a, "handle": p.handle, "job": p.job, "kind": w.kinds[a],
            "emotion": w.emotions[a].to_dict() if w.kinds[a] == "agent" else None,
            "posts": posts_by.get(a, 0), "followers": followers.get(a, 0), "following": following.get(a, 0),
            "communities": sorted(w.agent_comms[a]), "joined_period": w.joined[a],
            "archetype": w.archetypes[a] if w.kinds[a] == "agent" else None,
        })
    ids = w.agent_ids()
    labels = Counter(w.emotions[i].label for i in ids)
    topic_posts = Counter(p.topic for p in visible)
    story_posts = Counter(p.happening for p in visible if p.happening)
    return {
        "label": safety.SIM_LABEL, "synthetic": True, "name": w.name, "setting": "Maplebrook (fictional)",
        "period": w.period, "clock": round(w.clock, 1), "policy": w.policy, "mode": w.mode, "brain": w.brain,
        "human_id": w.human_id,
        "totals": {"residents": len(ids), "posts": sum(1 for p in visible if p.parent is None),
                   "replies": sum(1 for p in visible if p.parent is not None),
                   "votes": sum(len(p.voters) for p in w.sb.posts.values()), "follows": len(w.sb.follows),
                   "topics": len([t for t, n in w.sb.topics.counts.items() if n]),
                   "communities": len(w.communities)},
        "counters": {"moderated": w.sb.moderated, "quarantined": w.sb.quarantined,
                     "rejected": w.sb.rejected_actions},
        "trending": [{"topic": t, "score": v, "posts": topic_posts.get(t, 0), "community": w.topic_comm.get(t),
                      "happening": w._happening_brief(w._topic_happening(t))} for t, v in w.trending(10)],
        "happenings": [dict(h.to_dict(), posts=story_posts.get(h.id, 0)) for h in w.active_happenings()],
        "communities": comms, "agents": agents,
        "mood": {k: labels.get(k, 0) for k in emotions.LABELS},
        "latest_metrics": w.metrics[-1] if w.metrics else None,
        "round_progress": len(w.turns_done) / max(1, len(ids)),
    }


def _children(w) -> dict[int, list]:
    kids = defaultdict(list)
    for p in w.sb.posts.values():
        if p.visible and p.parent is not None:
            kids[p.parent].append(p)
    return kids


def _tree(w, post, kids, depth=0, max_depth=6) -> dict:
    out = w.post_out(post)
    out["children"] = ([_tree(w, c, kids, depth + 1, max_depth) for c in kids.get(post.id, [])]
                       if depth < max_depth else [])
    return out


def feed(w, sort: str = "hot", community: int | None = None, topic: str | None = None,
         author: int | None = None, limit: int = 25, offset: int = 0) -> dict:
    roots = [p for p in w.sb.posts.values() if p.visible and p.parent is None]
    if community == -1:
        roots = [p for p in roots if p.community is None]
    elif community is not None:
        roots = [p for p in roots if p.community == community]
    if topic:
        roots = [p for p in roots if p.topic == topic]
    if author is not None:
        roots = [p for p in roots if p.author == author]
    if sort == "new":
        roots.sort(key=lambda p: p.id, reverse=True)
    elif sort == "top":
        roots.sort(key=lambda p: (p.score + p.replies, p.id), reverse=True)
    else:
        roots.sort(key=lambda p: (_engagement(p, w.period), p.id), reverse=True)
    kids = _children(w)
    page = roots[offset:offset + limit]
    return {"total": len(roots), "posts": [_tree(w, p, kids, max_depth=2) for p in page]}


def thread(w, post_id: int) -> dict | None:
    p = w.sb.posts.get(post_id)
    if p is None or not p.visible:
        return None
    while p.parent is not None and p.parent in w.sb.posts and w.sb.posts[p.parent].visible:
        p = w.sb.posts[p.parent]
    return _tree(w, p, _children(w))


def agent(w, a: int) -> dict | None:
    if not 0 <= a < len(w.people):
        return None
    p = w.people[a]
    posts_by, followers, following = _counts(w)
    mine = [q for q in w.sb.posts.values() if q.visible and q.author == a][-30:][::-1]
    is_agent = w.kinds[a] == "agent"
    return {
        "id": a, "handle": p.handle, "job": p.job, "kind": w.kinds[a],
        "archetype": w.archetypes[a] if is_agent else None,
        "voice": culture.ARCHETYPES[w.archetypes[a]].voice if is_agent and w.archetypes[a] in culture.ARCHETYPES else "",
        "interests": p.interests, "values": p.values, "style": p.style,
        "openness": p.openness, "activity": p.activity, "joined_period": w.joined[a],
        "emotion": w.emotions[a].to_dict() if is_agent else None,
        "opinions": [{"topic": t, "value": v} for t, v in reversed(list(w.opinions[a].items()))],
        "memory": list(w.memory[a]) if is_agent else [],
        "communities": [{"id": c, "name": w.community_name(c)} for c in sorted(w.agent_comms[a])],
        "followers": [{"id": x, "handle": w.handles[x]} for (x, y) in w.sb.follows if y == a],
        "following": [{"id": y, "handle": w.handles[y]} for (x, y) in w.sb.follows if x == a],
        "followed_by_you": (w.human_id, a) in w.sb.follows,
        "stats": {"posts": posts_by.get(a, 0), "followers": followers.get(a, 0), "following": following.get(a, 0)},
        "recent_posts": [w.post_out(q) for q in mine],
    }


def network(w) -> dict:
    """Engagement graph with a cached spring layout and detected communities."""
    g = analysis.build_graph(len(w.people), w.sb)
    key = len(w.sb.interactions) // 25
    if w._layout_cache and w._layout_cache[0] == key and len(w._layout_cache[1]) == g.number_of_nodes():
        pos = w._layout_cache[1]
    else:
        prev = w._layout_cache[1] if w._layout_cache else None
        init = {n: prev[n] for n in g.nodes if prev and n in prev} or None
        pos = nx.spring_layout(g, seed=7, iterations=40, pos=init, k=1.6 / max(1, g.number_of_nodes()) ** 0.5)
        pos = {n: (float(x), float(y)) for n, (x, y) in pos.items()}
        w._layout_cache = (key, pos)
    label = {n: n for n in g.nodes}
    if g.number_of_edges():
        for i, c in enumerate(nx_comm.greedy_modularity_communities(g, weight="weight")):
            for n in c:
                label[n] = i
    pr = nx.pagerank(g, weight="weight") if g.number_of_edges() else {n: 1 / max(1, len(g)) for n in g}
    # Scale to the connected part of the graph; isolated residents are clamped to the edges
    # so a few loners can't squash everyone else into the middle.
    core = [n for n in g.nodes if g.degree(n) > 0] or list(g.nodes)
    xs = [pos[n][0] for n in core] or [0]
    ys = [pos[n][1] for n in core] or [0]
    sx, sy = (max(xs) - min(xs)) or 1, (max(ys) - min(ys)) or 1
    clamp = lambda v: round(min(1.0, max(0.0, v)), 4)  # noqa: E731
    nodes = [{"id": n, "handle": w.handles[n], "kind": w.kinds[n],
              "x": clamp((pos[n][0] - min(xs)) / sx), "y": clamp((pos[n][1] - min(ys)) / sy),
              "group": label[n], "pagerank": round(pr.get(n, 0), 5), "degree": g.degree(n),
              "emotion": w.emotions[n].label if w.kinds[n] == "agent" else "human"}
             for n in g.nodes]
    edges = [{"s": u, "t": v, "w": d["weight"]} for u, v, d in g.edges(data=True)]
    return {"nodes": nodes, "edges": edges}


def topics(w) -> dict:
    posts = [{"id": p.id, "author": p.author, "parent": p.parent, "tick": p.tick, "stance": p.stance,
              "topic": p.topic} for p in w.sb.posts.values() if p.visible]
    return {"table": analysis.topic_table(posts)[:40], "timeline": analysis.topic_timeline(posts, 8)}


def happenings(w) -> list[dict]:
    """Every story the newsroom produced, with its information cascade:
    posts and replies about it, distinct authors, and REACH (distinct residents
    whose feed showed at least one of those posts, from the exposure log)."""
    stats: dict[int, dict] = {}
    for p in w.sb.posts.values():
        if p.visible and p.happening:
            st = stats.setdefault(p.happening, {"posts": 0, "replies": 0, "authors": set(), "periods": Counter()})
            st["replies" if p.parent else "posts"] += 1
            st["authors"].add(p.author)
            st["periods"][p.tick] += 1
    reach = {r[0]: r[1] for r in w.conn.execute(
        "SELECT p.happening, COUNT(DISTINCT e.agent) FROM exposures e JOIN posts p ON p.id = e.post_id "
        "WHERE p.happening IS NOT NULL GROUP BY p.happening")}
    out = []
    for h in sorted(w.happenings.values(), key=lambda h: -h.id)[:60]:
        st = stats.get(h.id, {"posts": 0, "replies": 0, "authors": set(), "periods": Counter()})
        peak = st["periods"].most_common(1)[0][0] if st["periods"] else None
        out.append(dict(h.to_dict(), active=h.active, threads=st["posts"], replies=st["replies"],
                        authors=len(st["authors"]), reach=reach.get(h.id, 0), peak_period=peak))
    return out

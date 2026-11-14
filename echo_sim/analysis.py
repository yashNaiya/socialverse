"""Social-network analysis of a simulation state, with emergent topics.

Opinion metrics work per topic, because agents choose their own topics:
* polarization       — spread of opinions within each topic (weighted mean std)
* opinion_homophily  — how much MORE connected agents agree than random pairs
                       (> 0 means people engage with like-minded people: echo chamber)
* cross_cutting_ratio — share of engagement ties between agents on opposite
                       sides of at least one shared topic
* disagreement_rate  — share of replies that disagree with their parent
* topic metrics      — how many topics exist and how concentrated attention is
"""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from itertools import combinations

import networkx as nx
from networkx.algorithms import community

from .sandbox import Sandbox

POSITIVE = {"upvote", "comment", "follow"}


def build_graph(n_agents: int, sandbox: Sandbox) -> nx.Graph:
    """Undirected weighted graph of positive interactions (who engages with whom)."""
    g = nx.Graph()
    g.add_nodes_from(range(n_agents))
    for src, dst, kind, _ in sandbox.interactions:
        if kind not in POSITIVE or src == dst:
            continue
        w = g[src][dst]["weight"] + 1 if g.has_edge(src, dst) else 1
        g.add_edge(src, dst, weight=w)
    return g


def _r(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(x, 4)


def similarity(a: dict, b: dict) -> float | None:
    shared = a.keys() & b.keys()
    if not shared:
        return None
    return statistics.fmean(1 - abs(a[t] - b[t]) / 2 for t in shared)


def _opposed(a: dict, b: dict) -> bool | None:
    shared = [t for t in a.keys() & b.keys() if abs(a[t]) > 0.2 and abs(b[t]) > 0.2]
    if not shared:
        return None
    return any(a[t] * b[t] < 0 for t in shared)


def metrics(n_agents: int, sandbox: Sandbox, opinions: list[dict[str, float]]) -> dict:
    g = build_graph(n_agents, sandbox)
    visible = [p for p in sandbox.posts.values() if p.visible]
    threads = Counter(p.topic for p in visible if p.parent is None)
    total_threads = sum(threads.values())

    # Topic attention: normalized entropy (0 = everyone on one topic, 1 = spread evenly)
    concentration = None
    if len(threads) > 1:
        h = -sum((c / total_threads) * math.log(c / total_threads) for c in threads.values())
        concentration = 1 - h / math.log(len(threads))

    by_topic: dict[str, list[float]] = defaultdict(list)
    for o in opinions:
        for t, v in o.items():
            by_topic[t].append(v)
    groups = [vals for vals in by_topic.values() if len(vals) >= 3]
    polarization = (sum(statistics.pstdev(v) * len(v) for v in groups) / sum(len(v) for v in groups)
                    if groups else None)
    all_vals = [v for vals in by_topic.values() for v in vals]

    replies = [p.agreement for p in visible if p.parent is not None and p.agreement is not None]

    out = {
        "posts": len(sandbox.posts),
        "threads": total_threads,
        "n_topics": len(threads),
        "top_topic": threads.most_common(1)[0][0] if threads else None,
        "top_topic_share": _r(threads.most_common(1)[0][1] / total_threads) if threads else None,
        "topic_concentration": _r(concentration),
        "polarization": _r(polarization),
        "extremity": _r(statistics.fmean(abs(v) for v in all_vals)) if all_vals else None,
        "disagreement_rate": _r(sum(a < -0.2 for a in replies) / len(replies)) if replies else None,
        "edges": g.number_of_edges(),
        "density": round(nx.density(g), 4),
        "moderated": sandbox.moderated,
        "quarantined": sandbox.quarantined,
        "rejected_actions": sandbox.rejected_actions,
        "modularity": None,
        "communities": None,
        "opinion_homophily": None,
        "cross_cutting_ratio": None,
    }
    if g.number_of_edges() == 0:
        return out

    comms = community.greedy_modularity_communities(g, weight="weight")
    out["modularity"] = _r(community.modularity(g, comms, weight="weight"))
    out["communities"] = len(comms)

    edge_sims = [s for u, v in g.edges if (s := similarity(opinions[u], opinions[v])) is not None]
    base_sims = [s for u, v in combinations(range(n_agents), 2)
                 if (s := similarity(opinions[u], opinions[v])) is not None]
    if edge_sims and base_sims:
        out["opinion_homophily"] = _r(statistics.fmean(edge_sims) - statistics.fmean(base_sims))
    opp = [o for u, v in g.edges if (o := _opposed(opinions[u], opinions[v])) is not None]
    if opp:
        out["cross_cutting_ratio"] = _r(sum(opp) / len(opp))
    return out


def communities_of(n_agents: int, sandbox: Sandbox) -> list[int]:
    """Community label per agent (for colouring the network)."""
    g = build_graph(n_agents, sandbox)
    label = list(range(n_agents))
    if g.number_of_edges():
        for i, c in enumerate(community.greedy_modularity_communities(g, weight="weight")):
            for node in c:
                label[node] = i
    return label


def top_influencers(n_agents: int, sandbox: Sandbox, k: int = 5) -> list[tuple[int, float]]:
    g = build_graph(n_agents, sandbox)
    if g.number_of_edges() == 0:
        return []
    pr = nx.pagerank(g, weight="weight")
    return [(n, round(s, 4)) for n, s in sorted(pr.items(), key=lambda x: -x[1])[:k]]


def topic_table(posts: list[dict]) -> list[dict]:
    """Per-topic summary from post dicts (id, author, parent, tick, stance, topic)."""
    rows: dict[str, dict] = {}
    for p in posts:
        r = rows.setdefault(p["topic"], {"topic": p["topic"], "threads": 0, "replies": 0,
                                         "participants": set(), "first_round": p["tick"], "stances": []})
        r["threads" if not p["parent"] else "replies"] += 1
        r["participants"].add(p["author"])
        r["first_round"] = min(r["first_round"], p["tick"])
        r["stances"].append(p["stance"])
    out = []
    for r in rows.values():
        s = r.pop("stances")
        r["participants"] = len(r["participants"])
        r["mean_opinion"] = round(statistics.fmean(s), 3)
        r["polarization"] = round(statistics.pstdev(s), 3) if len(s) > 1 else 0.0
        out.append(r)
    return sorted(out, key=lambda r: -(r["threads"] + r["replies"]))


def topic_timeline(posts: list[dict], top_n: int = 8) -> list[dict]:
    """Posts+replies per round for the most active topics (for an area chart)."""
    totals = Counter(p["topic"] for p in posts)
    keep = {t for t, _ in totals.most_common(top_n)}
    per = defaultdict(Counter)
    for p in posts:
        per[p["tick"]][p["topic"] if p["topic"] in keep else "other"] += 1
    return [{"round": r, **per[r]} for r in sorted(per)]

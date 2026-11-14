"""A living Socialverse world: in-memory state with write-through persistence.

Time
* The world has a virtual clock in minutes. A *period* is 60 virtual minutes;
  metrics are snapshotted and communities can emerge at every period close.
* Continuous mode: every agent has its own next-action time (exponential
  inter-arrival, faster when it is active or emotionally aroused). The agent
  due soonest acts next; the world never pauses between periods.
* Round mode: within a period, every agent gets exactly one chance to act, in a
  seeded shuffled order (synchronized steps for controlled experiments).
  Resumes mid-round after a pause.

Thread safety: every read or write of state or the database happens under
`self.lock`. The LLM call happens outside the lock, so the UI and the human
participant stay responsive while an agent is "typing".
"""

from __future__ import annotations

import json
import random
import threading
import time
from collections import Counter, deque
from pathlib import Path

from echo_sim import analysis, config, engine, safety
from echo_sim import topics as topics_mod
from echo_sim.personas import generate
from echo_sim.sandbox import ActionError, Post, Sandbox

from . import culture, emotions, feed as feedmod, store
from .emotions import Emotion
from .hybrid import Planner, WriteContext

PERIOD_MINUTES = 60.0
MAX_COMMUNITIES = 12
COMMUNITY_MIN_THREADS = 4
COMMUNITY_MIN_AUTHORS = 3
MAX_NEW_COMMUNITIES_PER_PERIOD = 2
MAX_ACTIVE_HAPPENINGS = 8
HEAT_DECAY = 0.62            # per period: trends fade unless people keep posting
HEAT_PER_POST = 0.035        # each post about a happening keeps it alive (virality)
HEAT_CAP = 1.8
TREND_WINDOW = 3             # periods that count for "trending"


class HumanError(ValueError):
    """A human action that the platform refused (shown to the user)."""


class World:
    def __init__(self, name: str, root: Path = store.WORLDS_DIR, publish=None):
        self.name = store.validate_name(name)
        self.conn = store.open_world(name, root)
        try:
            self.token = store.acquire_lease(self.conn)
        except Exception:
            self.conn.close()
            raise
        self.lock = threading.RLock()
        self.publish = publish or (lambda msg: None)
        self.planner = Planner()
        self.closed = False
        with self.lock:
            self._load()
            if not self.happenings:                 # a new world starts with something going on
                with store.tx(self.conn):
                    store.check_lease(self.conn, self.token)
                    self._spawn_happenings(self.period, 3)

    # ================================================================ load
    def _load(self) -> None:
        d = store.load_rows(self.conn)
        meta = d["meta"]
        self.seed = int(meta.get("seed", 0))
        self.clock = float(meta.get("clock", 0.0))
        self.policy = meta.get("policy", "personalized")
        self.mode = meta.get("mode", "continuous")
        self.brain = meta.get("brain", "mock")
        self.human_id = int(meta.get("human_id", -1))
        self.people = [a["persona"] for a in d["agents"]]
        self.kinds = [a["kind"] for a in d["agents"]]
        self.opinions = [a["opinions"] for a in d["agents"]]
        self.memory = [a["memory"] for a in d["agents"]]
        self.inbox = [a["inbox"] for a in d["agents"]]
        self.emotions = [Emotion(a["valence"], a["arousal"]).clamp() for a in d["agents"]]
        self.next_due = [float(a["next_due"]) for a in d["agents"]]
        self.joined = [a["joined"] for a in d["agents"]]
        self.archetypes = [a["archetype"] for a in d["agents"]]
        self.happenings: dict[int, culture.Happening] = {h.id: h for h in d["happenings"]}
        self.handles = {p.id: p.handle for p in self.people}

        sb = Sandbox(len(self.people))
        for r in d["posts"]:
            post = Post(id=r["id"], author=r["author"], text=r["text"], stance=r["stance"], tick=r["period"],
                        parent=r["parent"], score=r["score"], replies=r["replies"], visible=r["visible"],
                        voters=r["voters"], topic=r["topic"], agreement=r["agreement"], community=r["community"],
                        format=r["format"],
                        happening=r["happening"] if r["happening"] in self.happenings else None)
            sb.posts[post.id] = post
            if post.visible:
                sb.topics.add_post(post.topic)
        sb._next_id = max(sb.posts, default=0) + 1
        sb.follows = d["follows"]
        sb.interactions = d["interactions"]
        sb.persisted_interactions = len(sb.interactions)
        sb.moderated = int(meta.get("moderated", 0))
        sb.quarantined = int(meta.get("quarantined", 0)) + d["hidden"]
        sb.rejected_actions = int(meta.get("rejected", 0))
        self.sb = sb

        self.communities = {c["id"]: c for c in d["communities"]}
        self.topic_comm = {c["topic"]: c["id"] for c in d["communities"]}
        self.members = {cid: set() for cid in self.communities}
        self.agent_comms: list[set[int]] = [set() for _ in self.people]
        for cid, a in d["members"]:
            if cid in self.members and 0 <= a < len(self.people):
                self.members[cid].add(a)
                self.agent_comms[a].add(cid)

        self.likes = [Counter() for _ in self.people]       # who each agent keeps liking
        for src, dst, kind, _ in sb.interactions:
            if kind in ("upvote", "comment") and src != dst:
                self.likes[src][dst] += 1

        self.turns_done = {a for (p, a) in d["turns"] if p == self.period}
        self.rng = random.Random(f"{self.seed}:{self.clock}:{len(sb.posts)}")
        self.period_actions: Counter = Counter()
        self.recent_events = deque(maxlen=250)
        for r in self.conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 250").fetchall()[::-1]:
            self.recent_events.append(self._event_out(dict(r)))
        self.metrics = [{"period": r["period"], "policy": r["policy"], "mode": r["mode"], "brain": r["brain"],
                         **json.loads(r["metrics_json"])}
                        for r in self.conn.execute("SELECT * FROM period_metrics ORDER BY period")]
        self._layout_cache: tuple[int, dict] | None = None

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
            try:
                store.release_lease(self.conn, self.token)
            finally:
                self.conn.close()

    # ================================================================ helpers
    @property
    def period(self) -> int:
        return int(self.clock // PERIOD_MINUTES) + 1

    def agent_ids(self) -> list[int]:
        return [i for i, k in enumerate(self.kinds) if k == "agent"]

    def _posts(self) -> list[Post]:
        return list(self.sb.posts.values())

    def _follow_candidates(self, a: int) -> list[int]:
        following = self.sb.following(a)
        return [b for b, c in self.likes[a].most_common(6) if c >= 2 and b != a and b not in following]

    def _joinable(self, a: int) -> list[int]:
        held = self.opinions[a]
        cands = [cid for topic, cid in self.topic_comm.items()
                 if topic in held and cid not in self.agent_comms[a]]
        return sorted(cands, key=lambda c: -len(self.members[c]))

    def active_happenings(self) -> list[culture.Happening]:
        return sorted((h for h in self.happenings.values() if h.active), key=lambda h: -h.heat)

    def _topic_happening(self, topic: str) -> int | None:
        for h in self.active_happenings():
            if h.topic == topic:
                return h.id
        return None

    def trending(self, n: int = 8) -> list[tuple[str, float]]:
        """What's hot RIGHT NOW: recent posts weighted by engagement and freshness."""
        now, score = self.period, Counter()
        for post in reversed(list(self.sb.posts.values())):      # newest first; ids grow with time
            if now - post.tick > TREND_WINDOW:
                break
            if post.visible and post.topic != topics_mod.FALLBACK:
                score[post.topic] += (1 + 0.3 * post.replies + 0.2 * max(0, post.score)) / (1 + now - post.tick)
        return [(t, round(v, 2)) for t, v in score.most_common(n)]

    def _spawn_happenings(self, p: int, k: int) -> None:
        avoid = {h.topic for h in self.active_happenings()}
        for _ in range(k):
            if len(self.active_happenings()) >= MAX_ACTIVE_HAPPENINGS:
                break
            hid = max(self.happenings, default=0) + 1
            busy = frozenset(x.extra.get("template", "") for x in self.active_happenings())
            h = culture.generate_happening(self.rng, hid, p, avoid, busy)
            if h is None:
                break
            h.topic = self.sb.topics.resolve(h.topic)
            avoid.add(h.topic)
            self.conn.execute("INSERT INTO happenings(id, category, headline, topic, sentiment, controversy, heat, "
                              "period, meme) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                              (h.id, h.category, h.headline, h.topic, h.sentiment, h.controversy, h.heat, p,
                               h.extra.get("meme", "")))
            self.happenings[h.id] = h
            out = {"period": p, "clock": round(self.clock, 2), "agent": -1, "handle": "Platform", "action": "news",
                   "target": h.id, "ok": True, "note": h.headline, "post_id": None, "topic": h.topic,
                   "stance_change": 0.0, "emotion": "", "human": False, "category": h.category}
            self.recent_events.append(out)
            self.publish({"type": "event", "event": out, "post": None})

    def _update_happenings(self, p: int) -> None:
        for h in self.happenings.values():
            if h.active:
                h.heat = round(h.heat * HEAT_DECAY, 4)
                self.conn.execute("UPDATE happenings SET heat = ? WHERE id = ?", (h.heat, h.id))
        active = len(self.active_happenings())
        k = self.rng.choice([0, 1, 1, 1, 2, 2, 3])
        if active < 2:
            k = max(k, 2 - active)
        self._spawn_happenings(p + 1, k)

    def community_name(self, cid: int | None) -> str | None:
        return f"c/{self.communities[cid]['topic']}" if cid in self.communities else None

    def _event_out(self, ev: dict) -> dict:
        a = ev.get("agent", 0)
        out = {k: ev.get(k) for k in ("period", "clock", "agent", "action", "target", "ok", "note",
                                      "post_id", "topic", "stance_change", "emotion", "human")}
        out["ok"] = bool(out["ok"])
        out["human"] = bool(out["human"])
        out["handle"] = self.handles.get(a, "?") if hasattr(self, "handles") else "?"
        return out

    def _bump_inbox(self, b: int, key: str, dirty: set[int]) -> None:
        if 0 <= b < len(self.inbox) and self.kinds[b] == "agent":
            self.inbox[b][key] = self.inbox[b].get(key, 0) + 1
            dirty.add(b)

    def _read_inbox(self, a: int) -> None:
        box = self.inbox[a]
        if not box:
            return
        emotions.feedback(self.emotions[a], box)
        parts = [f"{box[k]} {label}" for k, label in
                 (("up", "upvotes"), ("down", "downvotes"), ("agree", "agreeing replies"),
                  ("disagree", "disagreeing replies"), ("followers", "new followers")) if box.get(k)]
        if parts:
            self.memory[a].append(f"period {self.period}: since your last visit you got " + ", ".join(parts))
        self.inbox[a] = {}

    def _vote_opinion(self, a: int, post: Post, value: int) -> float:
        """Bounded confidence with a backfire effect for angry agents."""
        ops, o = self.opinions[a], self.people[a].openness
        held = ops.get(post.topic)
        if held is None:
            new = post.stance * (0.4 + 0.4 * o) if value > 0 else -post.stance * 0.4
        else:
            diff = post.stance - held
            if abs(diff) < 0.6 + 0.6 * o:
                new = held + o * 0.25 * diff
            elif self.emotions[a].label == "angry":
                new = held - 0.05 * (1 if diff > 0 else -1)
            else:
                new = held
        return engine.update_opinion(ops, post.topic, new)

    # ================================================================ agent turn
    def agent_turn(self, a: int, writer, round_period: int | None = None) -> dict:
        with self.lock:
            period = self.period
            persona, emo = self.people[a], self.emotions[a]
            self.sb.start_tick()
            self._read_inbox(a)
            following = self.sb.following(a)
            feed = feedmod.build_feed(self.policy, self._posts(), a, self.opinions[a], following,
                                      self.agent_comms[a], period, config.MAX_FEED_ITEMS)
            exposures = [(a, p.id, period, i, self.policy) for i, p in enumerate(feed)]
            emotions.expose(emo, feed, self.opinions[a])
            arch = culture.ARCHETYPES.get(self.archetypes[a], culture.ARCHETYPES["local regular"])
            bonus = {}
            for p in feed:                         # gossips chase gossip threads, meme lords chase memes...
                h = self.happenings.get(p.happening) if p.happening else None
                b = 0.25 * arch.affinity.get(h.category, 0.0) if h else 0.0
                if p.format in arch.formats:
                    b += 0.1
                if b:
                    bonus[p.id] = b
            plan = self.planner.plan(self.rng, persona, emo, self.opinions[a], feed, following,
                                     self._follow_candidates(a), self._joinable(a),
                                     post_bias=arch.post_bias, bonus=bonus)
            active = self.active_happenings()
            if plan.action == "post":
                h, hint, fmt = culture.choose_post(self.rng, arch.name, active, [t for t, _ in self.trending(5)])
                plan.happening, plan.topic_hint, plan.fmt = (h.id if h else None), (None if h else hint), fmt
            ctx = None
            if plan.action in ("post", "comment"):
                target = next((p for p in feed if p.id == plan.target_post), None)
                root = target
                while root is not None and root.parent is not None and root.parent in self.sb.posts:
                    root = self.sb.posts[root.parent]
                ctx = WriteContext(persona=persona, feeling=emo.label, opinions=dict(self.opinions[a]),
                                   memory=list(self.memory[a]), trending=[t for t, _ in self.sb.topics.top(5)],
                                   communities=[self.communities[c]["topic"] for c in self.agent_comms[a]],
                                   handles=dict(self.handles), glimpse=list(feed[:3]), target=target, root=root,
                                   archetype=arch.name, fmt=plan.fmt if plan.action == "post" else "reply",
                                   happening=self.happenings.get(plan.happening if plan.action == "post"
                                                                 else (target.happening if target else None)),
                                   topic_hint=plan.topic_hint, news=active[:5])
            write_seed = self.rng.random()

        draft = None
        if ctx is not None:                         # the only LLM call, outside the lock
            wrng = random.Random(write_seed)
            if plan.action == "post":
                draft = writer.write_post(ctx, wrng)
            elif ctx.target is not None:
                draft = writer.write_reply(ctx, wrng, plan.lean)

        with self.lock:
            if self.closed:
                return {}
            return self._apply(a, plan, draft, feed, exposures, round_period)

    def _apply(self, a, plan, draft, feed, exposures, round_period) -> dict:
        period, emo, sb = self.period, self.emotions[a], self.sb
        feed_ids = {p.id: p for p in feed}
        ev = {"period": period, "clock": round(self.clock, 2), "agent": a, "action": plan.action,
              "target": 0, "ok": True, "note": "", "post_id": None, "topic": "", "stance_change": 0.0,
              "emotion": emo.label, "human": False}
        ch = {"post": None, "vote": None, "follow": None, "join": None, "dirty": set()}
        q0, m0 = sb.quarantined, sb.moderated
        line = None
        try:
            if plan.action == "post":
                if draft is None:
                    ev.update(action="idle", note="no text written")
                else:
                    h = self.happenings.get(plan.happening) if plan.happening else None
                    if h is not None:
                        topic = h.topic                 # reacting to a story: stay on its topic
                    else:
                        topic = sb.topics.resolve(plan.topic_hint or draft.topic or topics_mod.FALLBACK)
                    post = sb.create_post(a, draft.text, draft.stance, period, topic=topic)
                    if post is not None:
                        post.community = self.topic_comm.get(post.topic)
                        fmt = draft.fmt or plan.fmt
                        post.format = fmt if fmt in culture.FORMATS else "post"
                        post.happening = h.id if h else self._topic_happening(post.topic)
                        self._heat(post.happening)
                        ev["stance_change"] = engine.update_opinion(self.opinions[a], post.topic, draft.stance)
                        ev.update(post_id=post.id, topic=post.topic, target=post.community or 0)
                        ch["post"] = post
                        line = f"period {period}: you posted about '{post.topic}': {post.text[:100]}"
            elif plan.action == "comment":
                target = feed_ids.get(plan.target_post)
                if target is None:
                    raise ActionError("target not in your feed")
                ev.update(target=target.id, topic=target.topic)
                if draft is None:
                    ev.update(action="idle", note="no text written")
                else:
                    ev["stance_change"] = engine.update_opinion(self.opinions[a], target.topic, draft.my_stance)
                    post = sb.create_post(a, draft.text, self.opinions[a][target.topic], period,
                                          parent=target.id, agreement=draft.agreement)
                    if post is not None:
                        post.community = target.community
                        post.format = "reply"
                        post.happening = target.happening
                        self._heat(post.happening)
                        ev["post_id"] = post.id
                        ch["post"] = post
                        if target.author != a:
                            self._bump_inbox(target.author, "agree" if draft.agreement >= 0 else "disagree", ch["dirty"])
                            if draft.agreement >= 0:
                                self.likes[a][target.author] += 1
                        line = f"period {period}: you replied to {self.handles.get(target.author)}: {post.text[:100]}"
            elif plan.action in ("upvote", "downvote"):
                target = feed_ids.get(plan.target_post)
                if target is None:
                    raise ActionError("target not in your feed")
                value = 1 if plan.action == "upvote" else -1
                sb.vote(a, target.id, value, period)
                ev.update(target=target.id, topic=target.topic,
                          stance_change=self._vote_opinion(a, target, value))
                ch["vote"] = (target.id, value)
                self._bump_inbox(target.author, "up" if value > 0 else "down", ch["dirty"])
                if value > 0:
                    self.likes[a][target.author] += 1
                line = f"period {period}: you {plan.action}d a post about '{target.topic}'"
            elif plan.action == "follow":
                b = plan.target_agent
                if b not in self._follow_candidates(a):     # code-chosen, re-validated
                    raise ActionError("not a follow candidate")
                sb.follow(a, b, period)
                ev["target"] = b
                ch["follow"] = b
                self._bump_inbox(b, "followers", ch["dirty"])
                line = f"period {period}: you followed {self.handles.get(b)}"
            elif plan.action == "join":
                cid = plan.community
                if cid not in self.communities or cid in self.agent_comms[a]:
                    raise ActionError("cannot join that community")
                self.members[cid].add(a)
                self.agent_comms[a].add(cid)
                ev.update(target=cid, topic=self.communities[cid]["topic"])
                ch["join"] = cid
                line = f"period {period}: you joined {self.community_name(cid)}"
        except ActionError as e:
            sb.rejected_actions += 1
            ev.update(ok=False, note=f"blocked: {e}")
        if sb.quarantined > q0:
            ev.update(ok=False, note="quarantined: looked like prompt injection")
            line = f"period {period}: your {plan.action} was removed by the platform"
        elif sb.moderated > m0:
            ev.update(ok=False, note="removed by moderation")
            line = f"period {period}: your {plan.action} was removed by moderation"
        if line:
            self.memory[a].append(line)
        self.memory[a][:] = self.memory[a][-config.MAX_MEMORY_ITEMS:]
        emotions.decay(emo, emotions.baseline(self.people[a].style))
        ev["emotion"] = emo.label
        ev["stance_change"] = round(float(ev["stance_change"] or 0), 3)

        notable = ev["action"] != "idle" or abs(ev["stance_change"]) > 0.05
        self._persist(a, ev if notable else None, ch, exposures, round_period)
        if round_period is not None:
            self.turns_done.add(a)
        self.period_actions[ev["action"]] += 1
        out = self._event_out(ev)
        if notable:
            self.recent_events.append(out)
            self.publish({"type": "event", "event": out,
                          "post": self.post_out(ch["post"]) if ch["post"] else None})
        return out

    def _heat(self, hid: int | None) -> None:
        h = self.happenings.get(hid) if hid else None
        if h is not None and h.active:
            h.heat = min(HEAT_CAP, h.heat + HEAT_PER_POST)

    # ================================================================ persistence
    def _persist(self, a: int, ev: dict | None, ch: dict, exposures, round_period) -> None:
        c, sb = self.conn, self.sb
        with store.tx(c):
            store.check_lease(c, self.token)
            p = ch["post"]
            if p is not None:
                c.execute("INSERT INTO posts(id, author, parent, community, period, clock, text, stance, topic, "
                          "agreement, format, happening) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (p.id, p.author, p.parent, p.community, p.tick, self.clock, p.text, p.stance, p.topic,
                           p.agreement, p.format, p.happening))
                if p.parent:
                    c.execute("UPDATE posts SET replies = replies + 1 WHERE id = ?", (p.parent,))
            if ch["vote"]:
                pid, val = ch["vote"]
                c.execute("INSERT INTO votes(post_id, agent, value, period) VALUES (?, ?, ?, ?)",
                          (pid, a, val, self.period))
                c.execute("UPDATE posts SET score = score + ? WHERE id = ?", (val, pid))
            if ch["follow"] is not None:
                c.execute("INSERT INTO follows(follower, followee, period) VALUES (?, ?, ?)",
                          (a, ch["follow"], self.period))
            if ch["join"] is not None:
                c.execute("INSERT INTO memberships(community, agent, period) VALUES (?, ?, ?)",
                          (ch["join"], a, self.period))
            new = sb.interactions[sb.persisted_interactions:]
            c.executemany("INSERT INTO interactions(src, dst, kind, period) VALUES (?, ?, ?, ?)", new)
            sb.persisted_interactions = len(sb.interactions)
            c.executemany("INSERT INTO exposures(agent, post_id, period, rank, policy) VALUES (?, ?, ?, ?, ?)",
                          exposures)
            e = self.emotions[a]
            c.execute("UPDATE agents SET opinions_json = ?, memory_json = ?, inbox_json = ?, valence = ?, "
                      "arousal = ?, next_due = ? WHERE id = ?",
                      (json.dumps(self.opinions[a]), json.dumps(self.memory[a]), json.dumps(self.inbox[a]),
                       e.valence, e.arousal, self.next_due[a], a))
            for b in ch["dirty"] - {a}:
                c.execute("UPDATE agents SET inbox_json = ? WHERE id = ?", (json.dumps(self.inbox[b]), b))
            if ev is not None:
                c.execute("INSERT INTO events(period, clock, agent, action, target, ok, note, post_id, topic, "
                          "stance_change, emotion, human, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (ev["period"], ev["clock"], a, ev["action"], int(ev["target"] or 0), int(ev["ok"]),
                           ev["note"][:200], ev["post_id"], (ev["topic"] or "")[:40], ev["stance_change"],
                           ev["emotion"], int(ev["human"]), time.time()))
            if round_period is not None:
                c.execute("INSERT OR IGNORE INTO turns(period, agent) VALUES (?, ?)", (round_period, a))
            self._save_counters()

    def _save_counters(self) -> None:
        store.set_meta(self.conn, "clock", self.clock)
        store.set_meta(self.conn, "moderated", self.sb.moderated)
        store.set_meta(self.conn, "quarantined", self.sb.quarantined)
        store.set_meta(self.conn, "rejected", self.sb.rejected_actions)

    def save_settings(self, **values) -> None:
        with self.lock, store.tx(self.conn):
            store.check_lease(self.conn, self.token)
            for k, v in values.items():
                if k in ("policy", "mode", "brain"):
                    setattr(self, k, v)
                store.set_meta(self.conn, k, v)

    # ================================================================ clock
    def step_continuous(self, writer) -> dict | None:
        with self.lock:
            ids = self.agent_ids()
            for i in ids:                           # e.g. after round mode: reschedule stale agents
                if self.next_due[i] < self.clock:
                    self.next_due[i] = self.clock + self.rng.uniform(0, 30)
            a = min(ids, key=lambda i: (self.next_due[i], i))
            t = self.next_due[a]
            if t >= self.period * PERIOD_MINUTES:
                self.close_period()
                return None
            self.clock = t
            rate = max(0.05, self.people[a].activity * self.emotions[a].activity_factor())
            self.next_due[a] = t + self.rng.expovariate(rate / PERIOD_MINUTES)
        return self.agent_turn(a, writer)

    def step_round(self, writer) -> dict | None:
        with self.lock:
            r = self.period
            order = self.agent_ids()
            random.Random(f"{self.seed}:order:{r}").shuffle(order)
            remaining = [a for a in order if a not in self.turns_done]
            if not remaining:
                self.close_period()
                return None
            a = remaining[0]
            i = len(order) - len(remaining)
            self.clock = max(self.clock, (r - 1) * PERIOD_MINUTES + PERIOD_MINUTES * i / len(order))
            p_act = min(1.0, self.people[a].activity * self.emotions[a].activity_factor())
            if random.Random(f"{self.seed}:active:{r}:{a}").random() > p_act:
                self.turns_done.add(a)
                with store.tx(self.conn):
                    store.check_lease(self.conn, self.token)
                    self.conn.execute("INSERT OR IGNORE INTO turns(period, agent) VALUES (?, ?)", (r, a))
                    self._save_counters()
                return None
        return self.agent_turn(a, writer, round_period=r)

    def close_period(self) -> dict:
        with self.lock:
            p = self.period
            m = analysis.metrics(len(self.people), self.sb, self.opinions)
            m.update(self._extra_metrics(p))
            with store.tx(self.conn):
                store.check_lease(self.conn, self.token)
                founded = self._emerge_communities(p)
                self._update_happenings(p)
                m["new_communities"] = founded
                m["n_communities"] = len(self.communities)
                self.conn.execute("INSERT OR REPLACE INTO period_metrics(period, policy, mode, brain, metrics_json, ts) "
                                  "VALUES (?, ?, ?, ?, ?, ?)",
                                  (p, self.policy, self.mode, self.brain, json.dumps(m), time.time()))
                self.conn.execute("DELETE FROM turns WHERE period <= ?", (p,))
                self.clock = p * PERIOD_MINUTES
                self._save_counters()
            self.turns_done.clear()
            self.period_actions.clear()
            row = {"period": p, "policy": self.policy, "mode": self.mode, "brain": self.brain, **m}
            self.metrics.append(row)
            self.publish({"type": "period", "metrics": row})
            return row

    def _extra_metrics(self, p: int) -> dict:
        ids = self.agent_ids()
        labels = Counter(self.emotions[i].label for i in ids)
        cross, total = 0, 0
        for r in self.conn.execute("SELECT agent, post_id FROM exposures WHERE period = ?", (p,)):
            post = self.sb.posts.get(r["post_id"])
            held = self.opinions[r["agent"]].get(post.topic) if post else None
            if held is None or abs(held) <= 0.2 or abs(post.stance) <= 0.2:
                continue
            total += 1
            cross += held * post.stance < 0
        in_comm = sum(1 for i in ids if self.agent_comms[i])
        fmts, attention = Counter(), Counter()
        for post in self.sb.posts.values():
            if post.visible and post.tick == p:
                fmts[post.format] += 1
                h = self.happenings.get(post.happening) if post.happening else None
                attention[h.category if h else "organic"] += 1
        n_posts = sum(attention.values())
        return {
            "emotions": {k: labels.get(k, 0) for k in emotions.LABELS},
            "mean_valence": round(sum(self.emotions[i].valence for i in ids) / max(1, len(ids)), 4),
            "mean_arousal": round(sum(self.emotions[i].arousal for i in ids) / max(1, len(ids)), 4),
            "cross_cutting_exposure": round(cross / total, 4) if total else None,
            "exposures": total,
            "community_membership": round(in_comm / max(1, len(ids)), 4),
            "actions": dict(self.period_actions),
            "formats": dict(fmts),
            "attention": dict(attention),
            "happening_share": round(1 - attention.get("organic", 0) / n_posts, 4) if n_posts else None,
            "active_happenings": len(self.active_happenings()),
        }

    def _emerge_communities(self, p: int) -> list[str]:
        """Communities are founded by agents around topics THEY made popular."""
        threads: Counter = Counter()
        authors: dict[str, Counter] = {}
        for post in self.sb.posts.values():
            if post.visible and post.parent is None and self.kinds[post.author] == "agent":
                threads[post.topic] += 1
                authors.setdefault(post.topic, Counter())[post.author] += 1
        founded = []
        for topic, n in threads.most_common():
            if len(self.communities) >= MAX_COMMUNITIES or len(founded) >= MAX_NEW_COMMUNITIES_PER_PERIOD:
                break
            if topic in self.topic_comm or topic == topics_mod.FALLBACK:
                continue
            if n < COMMUNITY_MIN_THREADS or len(authors[topic]) < COMMUNITY_MIN_AUTHORS:
                continue
            founder = authors[topic].most_common(1)[0][0]
            cid = max(self.communities, default=0) + 1
            self.conn.execute("INSERT INTO communities(id, topic, founder, period) VALUES (?, ?, ?, ?)",
                              (cid, topic, founder, p))
            self.conn.execute("INSERT INTO memberships(community, agent, period) VALUES (?, ?, ?)",
                              (cid, founder, p))
            self.conn.execute("UPDATE posts SET community = ? WHERE topic = ? AND community IS NULL", (cid, topic))
            self.communities[cid] = {"id": cid, "topic": topic, "founder": founder, "period": p}
            self.topic_comm[topic] = cid
            self.members[cid] = {founder}
            self.agent_comms[founder].add(cid)
            for post in self.sb.posts.values():
                if post.topic == topic and post.community is None:
                    post.community = cid
            self.memory[founder].append(f"period {p}: you founded the community c/{topic}")
            ev = {"period": p, "clock": round(self.clock, 2), "agent": founder, "action": "community",
                  "target": cid, "ok": True, "note": "", "post_id": None, "topic": topic,
                  "stance_change": 0.0, "emotion": self.emotions[founder].label, "human": False}
            self.conn.execute("INSERT INTO events(period, clock, agent, action, target, ok, note, post_id, topic, "
                              "stance_change, emotion, human, ts) VALUES (?, ?, ?, 'community', ?, 1, '', NULL, ?, 0, ?, 0, ?)",
                              (p, ev["clock"], founder, cid, topic, ev["emotion"], time.time()))
            self.conn.execute("UPDATE agents SET memory_json = ? WHERE id = ?",
                              (json.dumps(self.memory[founder][-config.MAX_MEMORY_ITEMS:]), founder))
            out = self._event_out(ev)
            self.recent_events.append(out)
            self.publish({"type": "event", "event": out, "post": None})
            founded.append(topic)
        return founded

    # ================================================================ residents
    def add_residents(self, count: int) -> int:
        with self.lock:
            if not 1 <= count <= config.MAX_AGENTS:
                raise HumanError("count out of range")
            if len(self.agent_ids()) + count > config.MAX_AGENTS:
                raise HumanError(f"a world can have at most {config.MAX_AGENTS} residents")
            start = len(self.people)
            newcomers = generate(count, self.seed, start_id=start, taken=set(self.handles.values()))
            with store.tx(self.conn):
                store.check_lease(self.conn, self.token)
                store.insert_agents(self.conn, newcomers, "agent", self.period, self.clock, f"{self.seed}:{start}")
                store.add_culture(self.conn, newcomers, self.seed)
            due = {r["id"]: r["next_due"] for r in
                   self.conn.execute("SELECT id, next_due FROM agents WHERE id >= ?", (start,))}
            for p in newcomers:
                self.people.append(p)
                self.kinds.append("agent")
                self.opinions.append({})
                self.memory.append([])
                self.inbox.append({})
                self.emotions.append(Emotion(emotions.baseline(p.style)).clamp())
                self.next_due.append(float(due[p.id]))
                self.joined.append(self.period)
                self.archetypes.append(culture.assign(self.seed, p.id)[0])
                self.agent_comms.append(set())
                self.likes.append(Counter())
                self.handles[p.id] = p.handle
            self.sb.n_agents = len(self.people)
            self.publish({"type": "residents", "count": count})
            return count

    # ================================================================ human participant
    def _human_begin(self) -> int:
        if self.human_id < 0:
            raise HumanError("this world has no human seat")
        self.sb._actions_this_tick.pop(self.human_id, None)   # humans are rate-limited at the API instead
        return self.human_id

    def _human_event(self, action, target=0, post=None, topic="", ok=True, note="") -> dict:
        ev = {"period": self.period, "clock": round(self.clock, 2), "agent": self.human_id, "action": action,
              "target": target, "ok": ok, "note": note, "post_id": post.id if post else None, "topic": topic,
              "stance_change": 0.0, "emotion": "", "human": True}
        return ev

    def _human_commit(self, ev: dict, ch: dict) -> dict:
        self._persist(self.human_id, ev, ch, [], None)
        out = self._event_out(ev)
        self.recent_events.append(out)
        self.publish({"type": "event", "event": out, "post": self.post_out(ch["post"]) if ch["post"] else None})
        return out

    def _blocked(self, q0: int, m0: int, action: str):
        if self.sb.quarantined > q0:
            ev = self._human_event(action, ok=False, note="quarantined: looked like prompt injection")
            self._human_commit(ev, {"post": None, "vote": None, "follow": None, "join": None, "dirty": set()})
            raise HumanError("Your post was blocked: it looks like instructions aimed at the AI agents "
                             "(prompt injection). Agents never see it.")
        if self.sb.moderated > m0:
            raise HumanError("Your post was removed by moderation.")
        raise HumanError("Your post could not be published.")

    def human_post(self, text: str, topic: str = "", community: int | None = None, stance: float = 0.0) -> dict:
        with self.lock:
            h = self._human_begin()
            if community is not None:
                if community not in self.communities:
                    raise HumanError("unknown community")
                topic = self.communities[community]["topic"]
            topic = self.sb.topics.resolve(topic or topics_mod.FALLBACK)
            q0, m0 = self.sb.quarantined, self.sb.moderated
            post = self.sb.create_post(h, text, stance, self.period, topic=topic)
            if post is None:
                self._blocked(q0, m0, "post")
            post.community = self.topic_comm.get(post.topic)
            post.happening = self._topic_happening(post.topic)
            self._heat(post.happening)
            ch = {"post": post, "vote": None, "follow": None, "join": None, "dirty": set()}
            return {"event": self._human_commit(self._human_event("post", post.community or 0, post, post.topic), ch),
                    "post": self.post_out(post)}

    def human_reply(self, post_id: int, text: str, agreement: float = 0.0) -> dict:
        with self.lock:
            h = self._human_begin()
            parent = self.sb.posts.get(post_id)
            if parent is None or not parent.visible:
                raise HumanError("that post does not exist")
            q0, m0 = self.sb.quarantined, self.sb.moderated
            try:
                post = self.sb.create_post(h, text, 0.0, self.period, parent=post_id, agreement=agreement)
            except ActionError as e:
                raise HumanError(str(e)) from e
            if post is None:
                self._blocked(q0, m0, "comment")
            post.community = parent.community
            post.format = "reply"
            post.happening = parent.happening
            ch = {"post": post, "vote": None, "follow": None, "join": None, "dirty": set()}
            self._bump_inbox(parent.author, "agree" if agreement >= 0 else "disagree", ch["dirty"])
            return {"event": self._human_commit(self._human_event("comment", post_id, post, parent.topic), ch),
                    "post": self.post_out(post)}

    def human_vote(self, post_id: int, value: int) -> dict:
        with self.lock:
            h = self._human_begin()
            try:
                self.sb.vote(h, post_id, value, self.period)
            except ActionError as e:
                raise HumanError(str(e)) from e
            post = self.sb.posts[post_id]
            ch = {"post": None, "vote": (post_id, value), "follow": None, "join": None, "dirty": set()}
            self._bump_inbox(post.author, "up" if value > 0 else "down", ch["dirty"])
            action = "upvote" if value > 0 else "downvote"
            out = self._human_commit(self._human_event(action, post_id, topic=post.topic), ch)
            return {"event": out, "score": post.score}

    def human_follow(self, agent_id: int) -> dict:
        with self.lock:
            h = self._human_begin()
            try:
                self.sb.follow(h, agent_id, self.period)
            except ActionError as e:
                raise HumanError(str(e)) from e
            ch = {"post": None, "vote": None, "follow": agent_id, "join": None, "dirty": set()}
            self._bump_inbox(agent_id, "followers", ch["dirty"])
            return {"event": self._human_commit(self._human_event("follow", agent_id), ch)}

    def human_join(self, community: int) -> dict:
        with self.lock:
            h = self._human_begin()
            if community not in self.communities:
                raise HumanError("unknown community")
            if community in self.agent_comms[h]:
                raise HumanError("already a member")
            self.members[community].add(h)
            self.agent_comms[h].add(community)
            ch = {"post": None, "vote": None, "follow": None, "join": community, "dirty": set()}
            return {"event": self._human_commit(
                self._human_event("join", community, topic=self.communities[community]["topic"]), ch)}

    # ================================================================ views (read under lock)
    def _happening_brief(self, hid: int | None) -> dict | None:
        h = self.happenings.get(hid) if hid else None
        return {"id": h.id, "category": h.category, "icon": culture.CATEGORY_ICON[h.category],
                "headline": h.headline} if h else None

    def post_out(self, p: Post) -> dict:
        return {"id": p.id, "author": p.author, "handle": self.handles.get(p.author, "?"),
                "human": p.author == self.human_id, "parent": p.parent, "community": p.community,
                "community_name": self.community_name(p.community), "period": p.tick, "text": p.text,
                "stance": round(p.stance, 3), "topic": p.topic, "agreement": p.agreement,
                "score": p.score, "replies": p.replies, "format": p.format,
                "happening": self._happening_brief(p.happening),
                "emotion": self.emotions[p.author].to_dict()["emoji"] if self.kinds[p.author] == "agent" else "🧑"}

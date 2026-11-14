import { useEffect, useState } from "react";
import { api, type PostT, type Profile } from "../api";
import { ARCHETYPE_ICON, Avatar, EMOTION_COLORS, fmt, PostBody, useWorld } from "../ctx";
import { PostCard } from "./Post";

export function ProfileDrawer({ id, onClose, version }: { id: number; onClose: () => void; version: number }) {
  const { world, toast, refresh, openProfile } = useWorld();
  const [p, setP] = useState<Profile | null>(null);
  useEffect(() => { api.agent(world, id).then(setP).catch(() => {}); }, [world, id, version]);
  const follow = async () => {
    try { await api.follow(world, id); toast(`You now follow ${p?.handle}`); refresh(); }
    catch (e) { toast((e as Error).message, true); }
  };
  return (
    <>
      <div className="overlay" onClick={onClose} />
      <aside className="drawer" role="dialog" aria-label="Profile">
        <button className="btn ghost sm close" onClick={onClose}>✕</button>
        {!p ? <div className="muted">Loading…</div> : (
          <div className="stack" style={{ gap: 16 }}>
            <div className="row" style={{ gap: 14 }}>
              <Avatar id={p.id} handle={p.handle} emoji={p.emotion?.emoji} size="lg" human={p.kind === "human"} />
              <div className="grow">
                <div style={{ fontWeight: 800, fontSize: 19 }}>{p.kind === "human" ? "You" : p.handle}</div>
                <div className="muted small">{p.kind === "human" ? "Human participant" : `${p.job} · joined in period ${p.joined_period}`}</div>
                <div className="row small" style={{ gap: 12, marginTop: 4 }}>
                  <span><b>{p.stats.posts}</b> posts</span><span><b>{p.stats.followers}</b> followers</span><span><b>{p.stats.following}</b> following</span>
                </div>
              </div>
            </div>
            {p.kind === "agent" && (
              <>
                <div className="row wrap" style={{ gap: 6 }}>
                  {!p.followed_by_you ? <button className="btn primary sm" onClick={follow}>＋ Follow</button> : <span className="chip pos">Following</span>}
                  {p.archetype && <span className="chip accent" title={p.voice}>{ARCHETYPE_ICON[p.archetype]} {p.archetype}</span>}
                  <span className="chip">✍️ {p.style}</span>
                  <span className="chip">open-minded {fmt(p.openness)}</span>
                  <span className="chip">activity {fmt(p.activity)}</span>
                </div>
                <div className="card panel">
                  <div className="section-title">Feeling</div>
                  <div className="row" style={{ gap: 10 }}>
                    <span style={{ fontSize: 28 }}>{p.emotion?.emoji}</span>
                    <div className="grow stack" style={{ gap: 6 }}>
                      <b style={{ color: EMOTION_COLORS[p.emotion?.label ?? "neutral"] }}>{p.emotion?.label}</b>
                      <Meter label="valence" v={((p.emotion?.valence ?? 0) + 1) / 2} />
                      <Meter label="arousal" v={p.emotion?.arousal ?? 0} />
                    </div>
                  </div>
                </div>
                <div>
                  <div className="section-title">Interests & values</div>
                  <div className="row wrap" style={{ gap: 6 }}>
                    {p.interests.map((i) => <span key={i} className="chip accent">{i}</span>)}
                    {p.values.map((v) => <span key={v} className="chip">values {v}</span>)}
                  </div>
                </div>
                <div>
                  <div className="section-title">Opinions (formed during the simulation)</div>
                  {p.opinions.length === 0 && <div className="muted small">No opinions yet.</div>}
                  <div className="stack" style={{ gap: 6 }}>
                    {p.opinions.slice(0, 10).map((o) => <OpinionBar key={o.topic} topic={o.topic} v={o.value} />)}
                  </div>
                </div>
                <div>
                  <div className="section-title">Memory (what this agent remembers)</div>
                  <ul className="small" style={{ margin: 0, paddingLeft: 18, color: "var(--text-2)" }}>
                    {p.memory.slice().reverse().map((m, i) => <li key={i}>{m}</li>)}
                  </ul>
                </div>
              </>
            )}
            {p.communities.length > 0 && (
              <div><div className="section-title">Communities</div>
                <div className="row wrap" style={{ gap: 6 }}>{p.communities.map((c) => <span key={c.id} className="chip accent">{c.name}</span>)}</div>
              </div>
            )}
            {p.followers.length > 0 && (
              <div><div className="section-title">Followers</div>
                <div className="row wrap" style={{ gap: 6 }}>
                  {p.followers.map((f) => <span key={f.id} className="chip click" onClick={() => openProfile(f.id)}>{f.handle}</span>)}
                </div>
              </div>
            )}
            <div>
              <div className="section-title">Recent posts</div>
              <div className="stack">
                {p.recent_posts.length === 0 && <div className="muted small">Nothing yet.</div>}
                {p.recent_posts.slice(0, 8).map((q) => <MiniPost key={q.id} post={q} />)}
              </div>
            </div>
          </div>
        )}
      </aside>
    </>
  );
}

function Meter({ label, v }: { label: string; v: number }) {
  return (
    <div className="row" style={{ gap: 8 }}>
      <span className="muted tiny" style={{ width: 50 }}>{label}</span>
      <div className="bar grow"><span style={{ width: `${Math.round(v * 100)}%`, background: "var(--accent)" }} /></div>
    </div>
  );
}

function OpinionBar({ topic, v }: { topic: string; v: number }) {
  const pct = Math.abs(v) * 50;
  return (
    <div className="row" style={{ gap: 8 }}>
      <span className="small grow" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>#{topic}</span>
      <div style={{ width: 150, height: 8, background: "var(--bg-soft)", borderRadius: 99, position: "relative" }}>
        <span style={{ position: "absolute", top: 0, bottom: 0, borderRadius: 99,
          left: v >= 0 ? "50%" : `${50 - pct}%`, width: `${pct}%`, background: v >= 0 ? "var(--pos)" : "var(--neg)" }} />
        <span style={{ position: "absolute", left: "50%", top: -2, bottom: -2, width: 1, background: "var(--text-3)" }} />
      </div>
      <span className="tiny muted" style={{ width: 34, textAlign: "right" }}>{v > 0 ? "+" : ""}{fmt(v)}</span>
    </div>
  );
}

function MiniPost({ post }: { post: PostT }) {
  const { openThread } = useWorld();
  return (
    <div className="card" style={{ padding: 10, cursor: "pointer" }} onClick={() => openThread(post.id)}>
      <div className="row wrap" style={{ gap: 6 }}>
        <span className="chip">#{post.topic}</span>
        {post.parent && <span className="chip">reply</span>}
        <span className="muted tiny">P{post.period} · score {post.score}</span>
      </div>
      <PostBody text={post.text} format={post.format} small />
    </div>
  );
}

export function ThreadModal({ id, onClose, version }: { id: number; onClose: () => void; version: number }) {
  const { world } = useWorld();
  const [t, setT] = useState<PostT | null | undefined>(undefined);
  useEffect(() => { api.thread(world, id).then(setT).catch(() => setT(null)); }, [world, id, version]);
  return (
    <>
      <div className="overlay" onClick={onClose} />
      <div className="modal" role="dialog" aria-label="Thread">
        <button className="btn ghost sm close" onClick={onClose}>✕</button>
        <div style={{ marginTop: 22 }}>
          {t === undefined ? <div className="muted">Loading…</div> : t === null ? <div className="muted">This post isn't available.</div> : <PostCard post={t} full />}
        </div>
      </div>
    </>
  );
}

export function People() {
  const { state, openProfile } = useWorld();
  const [sort, setSort] = useState<"followers" | "posts" | "mood">("followers");
  const agents = state.agents.filter((a) => a.kind === "agent").slice().sort((a, b) =>
    sort === "mood" ? (b.emotion?.valence ?? 0) - (a.emotion?.valence ?? 0) : (b[sort] as number) - (a[sort] as number));
  return (
    <div className="main">
      <div className="row spread wrap">
        <h2 style={{ margin: 0, fontSize: 21 }}>Residents <span className="muted" style={{ fontSize: 15 }}>{agents.length}</span></h2>
        <div className="segmented">
          {(["followers", "posts", "mood"] as const).map((s) => <button key={s} className={sort === s ? "on" : ""} onClick={() => setSort(s)}>{s}</button>)}
        </div>
      </div>
      <div className="people">
        {agents.map((a) => (
          <div key={a.id} className="card person" onClick={() => openProfile(a.id)}>
            <div className="row" style={{ gap: 10 }}>
              <Avatar id={a.id} handle={a.handle} emoji={a.emotion?.emoji} />
              <div className="grow" style={{ minWidth: 0 }}>
                <div style={{ fontWeight: 700, overflow: "hidden", textOverflow: "ellipsis" }}>{a.handle}</div>
                <div className="muted small">{a.job}</div>
                {a.archetype && <span className="chip" style={{ marginTop: 4 }}>{ARCHETYPE_ICON[a.archetype]} {a.archetype}</span>}
              </div>
            </div>
            <div className="row small" style={{ gap: 10, marginTop: 10, color: "var(--text-2)" }}>
              <span><b>{a.followers}</b> followers</span><span><b>{a.posts}</b> posts</span>
              <span style={{ marginLeft: "auto", color: EMOTION_COLORS[a.emotion?.label ?? "neutral"] }}>{a.emotion?.label}</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

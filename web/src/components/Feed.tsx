import { useEffect, useState } from "react";
import { api, type PostT } from "../api";
import { Avatar, useWorld } from "../ctx";
import { PostCard } from "./Post";

export type Filter = { community: number | null; topic: string | null };

function Composer({ filter }: { filter: Filter }) {
  const { world, state, toast, refresh } = useWorld();
  const [text, setText] = useState("");
  const [topic, setTopic] = useState("");
  const [community, setCommunity] = useState<number | null>(null);
  const [stance, setStance] = useState(0);
  const [busy, setBusy] = useState(false);
  useEffect(() => { setCommunity(filter.community && filter.community > 0 ? filter.community : null); }, [filter.community]);

  const send = async () => {
    setBusy(true);
    try {
      await api.post(world, text.trim(), topic.trim(), community, stance);
      setText(""); setTopic(""); setStance(0);
      toast("Posted. Residents will see it in their feeds.");
      refresh();
    } catch (e) { toast((e as Error).message, true); }
    setBusy(false);
  };

  return (
    <div className="card composer">
      <div className="composer-row">
        <Avatar id={state.human_id} handle="you" human />
        <div className="grow stack">
          <textarea className="textarea" maxLength={400} placeholder="Share something with Maplebrook…"
            value={text} onChange={(e) => setText(e.target.value)} />
          {text.length > 0 && (
            <div className="row wrap" style={{ gap: 8 }}>
              <select className="select" style={{ width: 190 }} value={community ?? ""}
                onChange={(e) => setCommunity(e.target.value ? Number(e.target.value) : null)}>
                <option value="">🏛️ Town Square</option>
                {state.communities.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
              </select>
              {community === null && (
                <input className="input" style={{ width: 170 }} maxLength={40} placeholder="Topic (optional)"
                  value={topic} onChange={(e) => setTopic(e.target.value)} />
              )}
              <div className="segmented" style={{ width: 230 }}>
                {[[-1, "Critical"], [0, "Neutral"], [1, "Positive"]].map(([v, l]) => (
                  <button key={v} className={stance === v ? "on" : ""} onClick={() => setStance(v as number)}>{l}</button>
                ))}
              </div>
            </div>
          )}
          <div className="row spread">
            <span className="muted tiny">Your posts pass the same safety filters as the agents' posts. {text.length}/400</span>
            <button className="btn primary" disabled={busy || text.trim().length < 2} onClick={send}>Post</button>
          </div>
        </div>
      </div>
    </div>
  );
}

export function Feed({ filter, setFilter, version }: { filter: Filter; setFilter: (f: Filter) => void; version: number }) {
  const { world, state } = useWorld();
  const [sort, setSort] = useState<"hot" | "new" | "top">("new");
  const [posts, setPosts] = useState<PostT[] | null>(null);
  const [total, setTotal] = useState(0);
  const [limit, setLimit] = useState(25);

  useEffect(() => {
    let live = true;
    api.feed(world, { sort, community: filter.community, topic: filter.topic, limit })
      .then((r) => { if (live) { setPosts(r.posts); setTotal(r.total); } })
      .catch(() => {});
    return () => { live = false; };
  }, [world, sort, filter.community, filter.topic, limit, version]);

  const comm = state.communities.find((c) => c.id === filter.community);
  const title = filter.topic ? `#${filter.topic}` : comm ? comm.name : filter.community === -1 ? "Town Square" : "Home";

  return (
    <div className="main">
      <div className="row spread wrap">
        <div>
          <h2 style={{ margin: 0, fontSize: 21, letterSpacing: "-.02em" }}>{title}</h2>
          <div className="muted small">
            {comm ? `Founded by ${comm.founder_handle} in period ${comm.founded_period} · ${comm.members} members · ${comm.posts} threads`
              : `${total} threads`}
            {(filter.topic || filter.community !== null) && (
              <> · <a href="#" onClick={(e) => { e.preventDefault(); setFilter({ community: null, topic: null }); }}>clear filter</a></>
            )}
          </div>
        </div>
        <div className="sorts">
          <div className="segmented">
            {(["new", "hot", "top"] as const).map((s) => (
              <button key={s} className={sort === s ? "on" : ""} onClick={() => setSort(s)}>
                {s === "new" ? "🆕 New" : s === "hot" ? "🔥 Hot" : "🏆 Top"}
              </button>
            ))}
          </div>
        </div>
      </div>
      <Composer filter={filter} />
      {posts === null ? <div className="empty">Loading…</div> :
        posts.length === 0 ? (
          <div className="card empty">
            <div className="big">🌱</div>
            <div style={{ fontWeight: 650, color: "var(--text-2)" }}>Nothing here yet</div>
            <div className="small">Press <b>Start</b> on the right and the residents will start posting about whatever is on their minds.</div>
          </div>
        ) : posts.map((p) => <PostCard key={p.id} post={p} />)}
      {posts && posts.length < total && (
        <button className="btn" style={{ alignSelf: "center" }} onClick={() => setLimit((l) => Math.min(100, l + 25))}>Load more</button>
      )}
    </div>
  );
}

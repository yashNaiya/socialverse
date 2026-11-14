import { useState } from "react";
import { api, type EventT } from "../api";
import { colorFor, EMOTION_COLORS, EMOTION_EMOJI, fmt, useWorld } from "../ctx";
import type { Filter } from "./Feed";

export function LeftSidebar({ filter, setFilter }: { filter: Filter; setFilter: (f: Filter) => void }) {
  const { state, world, toast, refresh } = useWorld();
  const is = (c: number | null) => filter.topic === null && filter.community === c;
  const join = async (id: number) => {
    try { await api.join(world, id); toast("Joined"); refresh(); } catch (e) { toast((e as Error).message, true); }
  };
  return (
    <aside className="sidebar">
      <div className="card panel">
        <button className={`nav-item ${is(null) ? "active" : ""}`} onClick={() => setFilter({ community: null, topic: null })}>🏠 Home</button>
        <button className={`nav-item ${is(-1) ? "active" : ""}`} onClick={() => setFilter({ community: -1, topic: null })}>🏛️ Town Square</button>
      </div>
      <div className="card panel">
        <div className="section-title">What's happening</div>
        {(state.happenings ?? []).length === 0 && <div className="muted small">Quiet day in Maplebrook.</div>}
        {(state.happenings ?? []).slice(0, 6).map((h) => (
          <div key={h.id} className={`happening ${filter.topic === h.topic ? "nav-item active" : ""}`}
            onClick={() => setFilter({ community: null, topic: h.topic })}>
            <span style={{ fontSize: 18 }}>{h.icon}</span>
            <div className="grow" style={{ minWidth: 0 }}>
              <div className="cat">{h.category} · {h.posts ?? 0} posts{h.heat > 1 ? " · 🔥 viral" : ""}</div>
              <div className="hl">{h.headline}</div>
              <div className="bar" style={{ marginTop: 5, height: 4 }}>
                <span style={{ width: `${Math.min(100, (h.heat / 1.8) * 100)}%`, background: "var(--accent)" }} />
              </div>
            </div>
          </div>
        ))}
      </div>
      <div className="card panel">
        <div className="section-title">Communities</div>
        {state.communities.length === 0 && (
          <div className="muted small">None yet. When residents keep posting about the same thing, one of them founds a community for it.</div>
        )}
        {state.communities.map((c) => (
          <div key={c.id} className="row" style={{ gap: 4 }}>
            <button className={`nav-item ${is(c.id) ? "active" : ""}`} onClick={() => setFilter({ community: c.id, topic: null })}>
              <span className="comm-icon" style={{ background: colorFor(c.topic) }}>{c.topic[0]?.toUpperCase()}</span>
              <span className="grow" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{c.name}</span>
              <span className="count">{c.members}</span>
            </button>
            {!c.joined && <button className="btn sm ghost" title="Join this community" onClick={() => join(c.id)}>＋</button>}
          </div>
        ))}
      </div>
      <div className="card panel">
        <div className="section-title">Trending now</div>
        {state.trending.length === 0 && <div className="muted small">Topics appear as residents post.</div>}
        <div className="stack" style={{ gap: 2 }}>
          {state.trending.map((t, i) => (
            <button key={t.topic} className={`nav-item ${filter.topic === t.topic ? "active" : ""}`}
              onClick={() => setFilter({ community: null, topic: t.topic })}>
              <span className="muted tiny" style={{ width: 14 }}>{i + 1}</span>
              <span className="grow" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {t.happening ? `${t.happening.icon} ` : ""}#{t.topic}
              </span>
              <span className="count">{t.posts}</span>
            </button>
          ))}
        </div>
      </div>
    </aside>
  );
}

export function ControlPanel() {
  const { state, world, toast, refresh } = useWorld();
  const run = state.run;
  const running = run.status === "running";
  const [brain, setBrain] = useState(state.brain || "mock");
  const [mode, setMode] = useState(state.mode || "continuous");
  const [policy, setPolicy] = useState(state.policy || "personalized");
  const [speed, setSpeed] = useState(run.speed ?? 1);
  const [periods, setPeriods] = useState<string>("");
  const [busy, setBusy] = useState(false);

  const start = async () => {
    setBusy(true);
    try {
      await api.run(world, { brain, mode, policy, speed, periods: periods ? Number(periods) : null });
      refresh();
    } catch (e) { toast((e as Error).message, true); }
    setBusy(false);
  };
  const pause = async () => { await api.pause(world).catch(() => {}); setTimeout(refresh, 300); };
  const add = async () => {
    try { await api.addResidents(world, 5); toast("5 new residents moved to Maplebrook"); refresh(); }
    catch (e) { toast((e as Error).message, true); }
  };

  return (
    <div className="card panel stack">
      <div className="row spread">
        <div className="section-title" style={{ margin: 0 }}>Simulation</div>
        <span className={`chip ${running ? "pos" : run.status === "error" ? "neg" : ""}`}>
          {running && <span className="dot live" />} {running ? "Live" : run.status === "error" ? "Error" : "Paused"}
        </span>
      </div>
      {run.error && <div className="error-banner">{run.error}</div>}
      <label className="f">Agent brain
        <div className="segmented">
          <button className={brain === "ollama" ? "on" : ""} disabled={running} onClick={() => setBrain("ollama")}>🧠 Local AI</button>
          <button className={brain === "mock" ? "on" : ""} disabled={running} onClick={() => setBrain("mock")}>⚙️ Rule-based</button>
        </div>
      </label>
      <label className="f">Time
        <div className="segmented">
          <button className={mode === "continuous" ? "on" : ""} disabled={running} onClick={() => setMode("continuous")}>⏱ Continuous</button>
          <button className={mode === "rounds" ? "on" : ""} disabled={running} onClick={() => setMode("rounds")}>🔁 Rounds</button>
        </div>
      </label>
      <label className="f">Feed algorithm
        <select className="select" value={policy} disabled={running} onChange={(e) => setPolicy(e.target.value)}>
          <option value="personalized">Personalized (filter bubble)</option>
          <option value="engagement">Engagement (hot)</option>
          <option value="chronological">Chronological</option>
        </select>
      </label>
      <div className="row" style={{ gap: 10 }}>
        <label className="f grow">Pace: {speed.toFixed(1)}s / action
          <input type="range" min={0} max={3} step={0.1} value={speed} disabled={running} onChange={(e) => setSpeed(Number(e.target.value))} />
        </label>
        <label className="f" style={{ width: 92 }}>Stop after
          <input className="input" inputMode="numeric" placeholder="∞ periods" value={periods} disabled={running}
            onChange={(e) => setPeriods(e.target.value.replace(/\D/g, "").slice(0, 3))} />
        </label>
      </div>
      {running
        ? <button className="btn" onClick={pause}>⏸ Pause</button>
        : <button className="btn primary" disabled={busy} onClick={start}>{busy ? "Starting…" : "▶ Start"}</button>}
      <div className="row spread small muted">
        <span>{state.totals.residents} residents</span>
        <button className="btn ghost sm" onClick={add} disabled={state.totals.residents >= 100}>＋ 5 newcomers</button>
      </div>
      {state.brain === "ollama" && run.llm.calls > 0 && (
        <div className="muted tiny">Local AI: {run.llm.calls} texts written · {run.llm.avg_seconds}s avg · {run.llm.errors} failed</div>
      )}
      {mode === "rounds" && <div className="bar"><span style={{ width: `${Math.round(state.round_progress * 100)}%`, background: "var(--accent)" }} /></div>}
    </div>
  );
}

const ICON: Record<string, string> = {
  news: "🗞️", post: "📝", comment: "💬", upvote: "▲", downvote: "▼", follow: "➕", join: "🚪", community: "🎉", idle: "👀",
};

function describe(e: EventT, comms: Record<number, string>): string {
  switch (e.action) {
    case "post": return `posted about #${e.topic}`;
    case "comment": return `replied in #${e.topic}`;
    case "upvote": return `upvoted a post on #${e.topic}`;
    case "downvote": return `downvoted a post on #${e.topic}`;
    case "follow": return `started following someone`;
    case "join": return `joined ${comms[e.target] ?? "a community"}`;
    case "community": return `founded c/${e.topic} 🎉`;
    case "news": return e.note;
    default: return `changed their mind on #${e.topic}`;
  }
}

export function Activity({ events }: { events: EventT[] }) {
  const { state, openProfile, openThread } = useWorld();
  const comms = Object.fromEntries(state.communities.map((c) => [c.id, c.name]));
  return (
    <div className="card panel">
      <div className="section-title">Live activity</div>
      <div className="activity">
        {events.length === 0 && <div className="muted small">Waiting for residents…</div>}
        {[...events].reverse().slice(0, 80).map((e, i) => (
          <div key={`${e.clock}-${e.agent}-${e.action}-${i}`} className="act" style={{ opacity: e.ok ? 1 : 0.85 }}
            onClick={() => e.post_id && openThread(e.post_id)}>
            <span className="act-icon">{e.ok ? ICON[e.action] ?? "•" : "🛡️"}</span>
            <span className="grow">
              {e.action === "news" ? <b>Breaking{e.category ? ` ${e.category}` : ""}:</b> :
                <b style={{ cursor: "pointer" }} onClick={(ev) => { ev.stopPropagation(); openProfile(e.agent); }}>{e.human ? "You" : e.handle}</b>}{" "}
              {e.ok ? describe(e, comms) : <span style={{ color: "var(--neg)" }}>{e.note}</span>}
              {e.emotion && !e.human && <span title={e.emotion}> {EMOTION_EMOJI[e.emotion]}</span>}
              {Math.abs(e.stance_change) > 0.05 && (
                <span className="chip" style={{ marginLeft: 4, padding: "0 6px" }}>{e.stance_change > 0 ? "▲" : "▼"} {fmt(Math.abs(e.stance_change))}</span>
              )}
            </span>
            <span className="muted tiny">P{e.period}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export function MoodPanel() {
  const { state } = useWorld();
  const total = Object.values(state.mood).reduce((a, b) => a + b, 0) || 1;
  const rows = Object.entries(state.mood).filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1]);
  const c = state.counters;
  return (
    <div className="card panel stack">
      <div className="section-title" style={{ margin: 0 }}>Town mood</div>
      {rows.map(([k, n]) => (
        <div key={k} className="row" style={{ gap: 8 }}>
          <span style={{ width: 20 }}>{EMOTION_EMOJI[k]}</span>
          <span className="small" style={{ width: 62 }}>{k}</span>
          <div className="bar grow"><span style={{ width: `${(100 * n) / total}%`, background: EMOTION_COLORS[k] }} /></div>
          <span className="muted tiny" style={{ width: 22, textAlign: "right" }}>{n}</span>
        </div>
      ))}
      <div className="row wrap" style={{ gap: 6 }}>
        <span className="chip warn" title="Posts that looked like prompt injection: never stored, never shown to any agent">🛡️ {c.quarantined} injections blocked</span>
        <span className="chip" title="Removed by the moderation filter">🚫 {c.moderated} moderated</span>
        <span className="chip" title="Actions the platform refused">⛔ {c.rejected} rejected</span>
      </div>
    </div>
  );
}

import { useEffect, useMemo, useState } from "react";
import { Area, AreaChart, CartesianGrid, Line, LineChart, ReferenceArea, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api, type Happening, type Metrics, type NetworkT, type TopicsT } from "../api";
import { EMOTION_COLORS, fmt, useWorld } from "../ctx";

const CAT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6"];
const OTHER = "#9a9a96";
const POLICY_TINT: Record<string, string> = { personalized: "#6250d6", engagement: "#eb6834", chronological: "#2a78d6" };

type Spec = { key: string; title: string; hint: string; domain?: [number, number] };
const SPECS: Spec[] = [
  { key: "modularity", title: "Modularity", hint: "How strongly the network splits into clusters. ↑ = echo chambers" },
  { key: "opinion_homophily", title: "Opinion homophily", hint: "Connected residents agree more than random pairs. > 0 = like-minded clustering" },
  { key: "cross_cutting_exposure", title: "Cross-cutting exposure", hint: "Share of feed impressions from the opposing side. ↓ = filter bubble", domain: [0, 1] },
  { key: "polarization", title: "Polarization", hint: "Spread of opinions within each topic" },
  { key: "mean_valence", title: "Mood (mean valence)", hint: "−1 unhappy … +1 happy", domain: [-1, 1] },
  { key: "topic_concentration", title: "Topic concentration", hint: "1 = everyone talks about one thing", domain: [0, 1] },
  { key: "happening_share", title: "Driven by news & trends", hint: "Share of posts reacting to stories, gossip, memes and trends", domain: [0, 1] },
];

// Nine categories would exceed the validated palette, so they fold into five groups.
const ATTENTION_GROUPS: { key: string; label: string; cats: string[]; color: string }[] = [
  { key: "organic", label: "Own life & interests", cats: ["organic"], color: "#2a78d6" },
  { key: "news", label: "News & local", cats: ["news", "local"], color: "#eb6834" },
  { key: "celebs", label: "Gossip & entertainment", cats: ["gossip", "entertainment"], color: "#1baf7a" },
  { key: "memes", label: "Memes & trends", cats: ["meme", "trend"], color: "#eda100" },
  { key: "sports", label: "Sports & products", cats: ["sports", "product"], color: "#e87ba4" },
];

function AttentionChart({ data }: { data: Metrics[] }) {
  const rows = data.map((m) => {
    const a = m.attention ?? {};
    const row: Record<string, number> = { period: m.period };
    ATTENTION_GROUPS.forEach((g) => { row[g.key] = g.cats.reduce((t, c) => t + (a[c] ?? 0), 0); });
    return row;
  });
  return (
    <div className="card chart-card">
      <h3>What people talk about</h3>
      <p>Posts per period, by what they react to</p>
      <div className="row wrap" style={{ gap: 12, marginBottom: 6 }}>
        {ATTENTION_GROUPS.map((g) => <span key={g.key} className="row tiny" style={{ gap: 4 }}><span className="dot" style={{ background: g.color }} />{g.label}</span>)}
      </div>
      <ResponsiveContainer width="100%" height={200}>
        <AreaChart data={rows} margin={{ top: 4, right: 8, bottom: 0, left: -18 }}>
          <CartesianGrid stroke="var(--border)" vertical={false} />
          <XAxis dataKey="period" tick={{ fontSize: 11, fill: "var(--text-3)" }} stroke="var(--border)" />
          <YAxis tick={{ fontSize: 11, fill: "var(--text-3)" }} stroke="var(--border)" width={46} allowDecimals={false} />
          <Tooltip content={<TooltipBox digits={0} />} />
          {ATTENTION_GROUPS.map((g) => (
            <Area key={g.key} type="monotone" dataKey={g.key} name={g.label} stackId="1" stroke="var(--bg-elev)"
              strokeWidth={1.5} fill={g.color} fillOpacity={0.9} isAnimationActive={false} />
          ))}
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

function Cascades({ items }: { items: Happening[] }) {
  return (
    <div className="card chart-card table-wrap">
      <h3>Information cascades</h3>
      <p>How far each story spread. Reach = residents whose feed showed at least one post about it (from the exposure log).</p>
      <table className="t">
        <thead><tr><th>Story</th><th>Threads</th><th>Replies</th><th>Authors</th><th>Reach</th><th>Peak</th><th>Status</th></tr></thead>
        <tbody>
          {items.length === 0 && <tr><td colSpan={7} className="muted">No stories yet</td></tr>}
          {items.slice(0, 15).map((h) => (
            <tr key={h.id}>
              <td><span style={{ marginRight: 6 }}>{h.icon}</span>{h.headline}<div className="muted tiny">#{h.topic} · period {h.period}</div></td>
              <td>{h.threads}</td><td>{h.replies}</td><td>{h.authors}</td><td><b>{h.reach}</b></td>
              <td>{h.peak_period ?? "–"}</td>
              <td>{h.active ? <span className="chip pos">{h.heat > 1 ? "🔥 viral" : "live"}</span> : <span className="chip">faded</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function policyBands(m: Metrics[]) {
  const bands: { x1: number; x2: number; policy: string }[] = [];
  for (const r of m) {
    const last = bands[bands.length - 1];
    if (last && last.policy === r.policy) last.x2 = r.period;
    else bands.push({ x1: r.period, x2: r.period, policy: r.policy });
  }
  return bands;
}

function TooltipBox({ active, payload, label, digits = 3 }: any) {
  if (!active || !payload?.length) return null;
  return (
    <div className="card" style={{ padding: "8px 10px", fontSize: 12.5 }}>
      <div className="muted tiny">Period {label}{payload[0]?.payload?.policy ? ` · ${payload[0].payload.policy}` : ""}</div>
      {payload.map((p: any) => (
        <div key={p.dataKey} className="row" style={{ gap: 6 }}>
          <span className="dot" style={{ background: p.color }} /> <span>{p.name}</span>
          <b style={{ marginLeft: "auto" }}>{typeof p.value === "number" ? p.value.toFixed(digits) : "–"}</b>
        </div>
      ))}
    </div>
  );
}

function MetricChart({ spec, data, bands }: { spec: Spec; data: Metrics[]; bands: ReturnType<typeof policyBands> }) {
  const last = [...data].reverse().find((d) => typeof d[spec.key] === "number");
  return (
    <div className="card chart-card">
      <div className="row spread"><h3>{spec.title}</h3><b style={{ fontVariantNumeric: "tabular-nums" }}>{fmt(last?.[spec.key])}</b></div>
      <p>{spec.hint}</p>
      <ResponsiveContainer width="100%" height={150}>
        <LineChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: -18 }}>
          {bands.length > 1 && bands.map((b, i) => (
            <ReferenceArea key={i} x1={b.x1} x2={b.x2} fill={POLICY_TINT[b.policy]} fillOpacity={0.07} stroke="none" />
          ))}
          <CartesianGrid stroke="var(--border)" vertical={false} />
          <XAxis dataKey="period" tick={{ fontSize: 11, fill: "var(--text-3)" }} stroke="var(--border)" />
          <YAxis domain={spec.domain ?? ["auto", "auto"]} tick={{ fontSize: 11, fill: "var(--text-3)" }} stroke="var(--border)" width={46} />
          <Tooltip content={<TooltipBox />} cursor={{ stroke: "var(--text-3)", strokeDasharray: "3 3" }} />
          <Line type="monotone" dataKey={spec.key} name={spec.title} stroke="#2a78d6" strokeWidth={2} dot={false}
            activeDot={{ r: 5, stroke: "var(--bg-elev)", strokeWidth: 2 }} connectNulls isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

function MoodChart({ data }: { data: Metrics[] }) {
  const labels = ["happy", "excited", "calm", "curious", "neutral", "anxious", "sad", "angry"];
  const rows = data.map((m) => ({ period: m.period, ...(m.emotions ?? {}) }));
  const present = labels.filter((l) => rows.some((r: any) => r[l] > 0));
  return (
    <div className="card chart-card">
      <h3>Emotions over time</h3>
      <p>Residents per emotion at the end of each period</p>
      <div className="row wrap" style={{ gap: 10, marginBottom: 6 }}>
        {present.map((l) => <span key={l} className="row tiny" style={{ gap: 4 }}><span className="dot" style={{ background: EMOTION_COLORS[l] }} />{l}</span>)}
      </div>
      <ResponsiveContainer width="100%" height={180}>
        <AreaChart data={rows} margin={{ top: 4, right: 8, bottom: 0, left: -18 }}>
          <CartesianGrid stroke="var(--border)" vertical={false} />
          <XAxis dataKey="period" tick={{ fontSize: 11, fill: "var(--text-3)" }} stroke="var(--border)" />
          <YAxis tick={{ fontSize: 11, fill: "var(--text-3)" }} stroke="var(--border)" width={46} allowDecimals={false} />
          <Tooltip content={<TooltipBox digits={0} />} />
          {present.map((l) => (
            <Area key={l} type="monotone" dataKey={l} name={l} stackId="1" stroke="var(--bg-elev)" strokeWidth={1.5}
              fill={EMOTION_COLORS[l]} fillOpacity={0.9} isAnimationActive={false} />
          ))}
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

function NetworkGraph({ net, colorBy }: { net: NetworkT; colorBy: "group" | "emotion" }) {
  const { openProfile } = useWorld();
  const [hover, setHover] = useState<number | null>(null);
  const W = 640, H = 420, pad = 24;
  const pos = useMemo(() => Object.fromEntries(net.nodes.map((n) => [n.id, { x: pad + n.x * (W - 2 * pad), y: pad + n.y * (H - 2 * pad) }])), [net]);
  const groupSizes = useMemo(() => {
    const c: Record<number, number> = {};
    net.nodes.forEach((n) => { if (n.degree > 0) c[n.group] = (c[n.group] ?? 0) + 1; });
    return Object.entries(c).sort((a, b) => b[1] - a[1]).map(([g]) => Number(g));
  }, [net]);
  const colorOf = (n: NetworkT["nodes"][0]) => {
    if (n.kind === "human") return "#151826";
    if (colorBy === "emotion") return EMOTION_COLORS[n.emotion] ?? OTHER;
    const rank = groupSizes.indexOf(n.group);
    return rank >= 0 && rank < CAT.length ? CAT[rank] : OTHER;   // big clusters keep a fixed hue; the rest fold to "other"
  };
  const maxW = Math.max(1, ...net.edges.map((e) => e.w));
  const maxPr = Math.max(1e-6, ...net.nodes.map((n) => n.pagerank));
  const h = net.nodes.find((n) => n.id === hover);
  const neighbours = new Set(net.edges.flatMap((e) => (e.s === hover ? [e.t] : e.t === hover ? [e.s] : [])));
  return (
    <div style={{ position: "relative" }}>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: "auto", display: "block" }} role="img" aria-label="Interaction network">
        {net.edges.map((e, i) => {
          const a = pos[e.s], b = pos[e.t];
          const on = hover !== null && (e.s === hover || e.t === hover);
          return <line key={i} x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke={on ? "var(--text-2)" : "var(--text-3)"}
            strokeOpacity={hover === null ? 0.25 + 0.4 * (e.w / maxW) : on ? 0.9 : 0.06} strokeWidth={0.6 + 2 * (e.w / maxW)} />;
        })}
        {net.nodes.map((n) => {
          const p = pos[n.id];
          const r = 4 + 12 * Math.sqrt(n.pagerank / maxPr);
          const dim = hover !== null && hover !== n.id && !neighbours.has(n.id);
          return (
            <circle key={n.id} cx={p.x} cy={p.y} r={r} fill={colorOf(n)} stroke="var(--bg-elev)" strokeWidth={2}
              opacity={dim ? 0.25 : n.degree === 0 ? 0.45 : 1} style={{ cursor: "pointer", transition: "opacity .15s" }}
              onMouseEnter={() => setHover(n.id)} onMouseLeave={() => setHover(null)} onClick={() => openProfile(n.id)} />
          );
        })}
      </svg>
      {h && (
        <div className="card" style={{ position: "absolute", left: 10, top: 10, padding: "8px 10px", fontSize: 12.5, pointerEvents: "none" }}>
          <b>{h.kind === "human" ? "You" : h.handle}</b>
          <div className="muted tiny">{h.degree} connections · influence {(h.pagerank * 100).toFixed(1)}% · {h.emotion}</div>
        </div>
      )}
    </div>
  );
}

export function Observatory({ version }: { version: number }) {
  const { world, state } = useWorld();
  const [metrics, setMetrics] = useState<Metrics[]>([]);
  const [net, setNet] = useState<NetworkT | null>(null);
  const [topics, setTopics] = useState<TopicsT | null>(null);
  const [stories, setStories] = useState<Happening[]>([]);
  const [colorBy, setColorBy] = useState<"group" | "emotion">("group");

  useEffect(() => {
    api.metrics(world).then(setMetrics).catch(() => {});
    api.network(world).then(setNet).catch(() => {});
    api.topics(world).then(setTopics).catch(() => {});
    api.happenings(world).then(setStories).catch(() => {});
  }, [world, version]);

  const bands = policyBands(metrics);
  const m = state.latest_metrics;
  const kpi = (label: string, v: any, hint: string, d = 2) => (
    <div className="card kpi"><div className="l">{label}</div><div className="v">{typeof v === "number" ? v.toFixed(d) : v ?? "–"}</div><div className="h">{hint}</div></div>
  );

  return (
    <div className="main" style={{ gap: 16 }}>
      <div>
        <h2 style={{ margin: 0, fontSize: 21 }}>Observatory</h2>
        <div className="muted small">Social-network analysis of the live world, snapshotted at the end of every period (60 virtual minutes).</div>
      </div>
      <div className="kpis">
        {kpi("Period", state.period, `${state.mode} · ${state.policy}`, 0)}
        {kpi("Communities", state.totals.communities, "founded by agents", 0)}
        {kpi("Modularity", m?.modularity, "cluster strength")}
        {kpi("Homophily", m?.opinion_homophily, "like-minded ties")}
        {kpi("Cross-cutting", m?.cross_cutting_exposure, "opposing views seen")}
        {kpi("Injections blocked", state.counters.quarantined, "never shown to agents", 0)}
      </div>
      {metrics.length < 2 ? (
        <div className="card empty"><div className="big">📈</div>Charts appear after two periods. Start the simulation and give it a minute.</div>
      ) : (
        <>
          {bands.length > 1 && (
            <div className="row wrap small" style={{ gap: 12 }}>
              <span className="muted">Shaded by feed algorithm:</span>
              {Object.entries(POLICY_TINT).map(([p, c]) => <span key={p} className="row" style={{ gap: 4 }}><span className="dot" style={{ background: c, opacity: 0.5 }} />{p}</span>)}
            </div>
          )}
          <div className="grid2">{SPECS.map((s) => <MetricChart key={s.key} spec={s} data={metrics} bands={bands} />)}</div>
          <AttentionChart data={metrics} />
          <MoodChart data={metrics} />
        </>
      )}
      <div className="card chart-card">
        <div className="row spread wrap">
          <div><h3>Who interacts with whom</h3><p>Node size = influence (PageRank). Edges = replies, upvotes, follows. Hover to highlight, click to open a profile.</p></div>
          <div className="segmented">
            <button className={colorBy === "group" ? "on" : ""} onClick={() => setColorBy("group")}>Clusters</button>
            <button className={colorBy === "emotion" ? "on" : ""} onClick={() => setColorBy("emotion")}>Emotion</button>
          </div>
        </div>
        {net && net.edges.length > 0 ? <NetworkGraph net={net} colorBy={colorBy} /> : <div className="empty">No interactions yet.</div>}
      </div>
      <Cascades items={stories} />
      <div className="grid2">
        <div className="card chart-card table-wrap">
          <h3>Communities</h3><p>Emerged from what agents chose to talk about</p>
          <table className="t">
            <thead><tr><th>Community</th><th>Founder</th><th>Members</th><th>Threads</th><th>Mood</th><th>Opinion</th></tr></thead>
            <tbody>
              {state.communities.length === 0 && <tr><td colSpan={6} className="muted">None yet</td></tr>}
              {state.communities.map((c) => (
                <tr key={c.id}><td><b>{c.name}</b><div className="muted tiny">period {c.founded_period}</div></td><td>{c.founder_handle}</td>
                  <td>{c.members}</td><td>{c.posts}</td><td>{fmt(c.mood)}</td><td>{fmt(c.mean_opinion)}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="card chart-card table-wrap">
          <h3>Topics</h3><p>Chosen by the agents themselves; near-duplicates merged</p>
          <table className="t">
            <thead><tr><th>Topic</th><th>Threads</th><th>Replies</th><th>People</th><th>Mean</th><th>Spread</th></tr></thead>
            <tbody>
              {(topics?.table ?? []).slice(0, 12).map((t) => (
                <tr key={t.topic}><td>#{t.topic}</td><td>{t.threads}</td><td>{t.replies}</td><td>{t.participants}</td><td>{fmt(t.mean_opinion)}</td><td>{fmt(t.polarization)}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      <div className="card chart-card table-wrap">
        <h3>Period log</h3><p>The data behind the charts</p>
        <table className="t">
          <thead><tr><th>Period</th><th>Feed</th><th>Mode</th><th>Brain</th><th>Posts</th><th>Modularity</th><th>Homophily</th><th>Cross-cut</th><th>Valence</th></tr></thead>
          <tbody>
            {[...metrics].reverse().slice(0, 30).map((r) => (
              <tr key={r.period}><td>{r.period}</td><td>{r.policy}</td><td>{r.mode}</td><td>{r.brain}</td><td>{r.posts}</td>
                <td>{fmt(r.modularity)}</td><td>{fmt(r.opinion_homophily)}</td><td>{fmt(r.cross_cutting_exposure)}</td><td>{fmt(r.mean_valence)}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

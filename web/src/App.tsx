import { useCallback, useEffect, useMemo, useState } from "react";
import { api, type EventT, type State, type WorldInfo } from "./api";
import { periodLabel, WorldCtx } from "./ctx";
import { useLive, useThrottle, type LiveMsg } from "./useLive";
import { Feed, type Filter } from "./components/Feed";
import { Observatory } from "./components/Observatory";
import { People, ProfileDrawer, ThreadModal } from "./components/Overlays";
import { Activity, ControlPanel, LeftSidebar, MoodPanel } from "./components/Sidebars";

function useToast() {
  const [t, setT] = useState<{ msg: string; err: boolean } | null>(null);
  useEffect(() => { if (!t) return; const id = setTimeout(() => setT(null), 4200); return () => clearTimeout(id); }, [t]);
  return { toast: t, show: (msg: string, err = false) => setT({ msg, err }) };
}

function ThemeToggle() {
  const [theme, setTheme] = useState<string>(() => { try { return localStorage.getItem("sv-theme") ?? ""; } catch { return ""; } });
  useEffect(() => {
    if (theme) document.documentElement.dataset.theme = theme; else delete document.documentElement.dataset.theme;
    try { localStorage.setItem("sv-theme", theme); } catch { /* private mode */ }
  }, [theme]);
  const dark = theme === "dark" || (!theme && matchMedia("(prefers-color-scheme: dark)").matches);
  return <button className="btn ghost sm" title="Toggle theme" onClick={() => setTheme(dark ? "light" : "dark")}>{dark ? "☀️" : "🌙"}</button>;
}

function Landing({ onOpen }: { onOpen: (name: string) => void }) {
  const [worlds, setWorlds] = useState<WorldInfo[] | null>(null);
  const [name, setName] = useState("maplebrook");
  const [residents, setResidents] = useState(40);
  const [err, setErr] = useState("");
  useEffect(() => { api.worlds().then(setWorlds).catch(() => setWorlds([])); }, []);
  const create = async () => {
    setErr("");
    try { await api.create(name, residents, Math.floor(Math.random() * 1e6)); onOpen(name); }
    catch (e) { setErr((e as Error).message); }
  };
  return (
    <div className="landing">
      <div className="row spread">
        <div className="brand"><div className="brand-mark">◎</div>Socialverse</div>
        <ThemeToggle />
      </div>
      <div className="hero">
        <span className="chip accent">AI social-network observatory · runs 100% on your computer</span>
        <h1>A living social network<br /><span className="gradient-text">of AI residents</span></h1>
        <p>Residents of the fictional town of Maplebrook choose their own topics, form opinions, feel emotions,
          follow each other and found communities, while you watch how the feed algorithm shapes the network. Join in as a human, too.</p>
      </div>
      <div className="card panel stack" style={{ marginTop: 24, maxWidth: 560 }}>
        <div className="section-title" style={{ margin: 0 }}>Create a new world</div>
        <div className="row wrap" style={{ gap: 10, alignItems: "flex-end" }}>
          <label className="f grow">Name<input className="input" value={name} maxLength={40} onChange={(e) => setName(e.target.value.replace(/[^A-Za-z0-9_-]/g, ""))} /></label>
          <label className="f" style={{ width: 120 }}>Residents<input className="input" type="number" min={2} max={100} value={residents} onChange={(e) => setResidents(Math.max(2, Math.min(100, Number(e.target.value) || 2)))} /></label>
          <button className="btn primary" disabled={!name} onClick={create}>Create world</button>
        </div>
        {err && <div className="error-banner">{err}</div>}
      </div>
      <h3 style={{ marginTop: 34, marginBottom: 0 }}>Your worlds</h3>
      <div className="worlds">
        {worlds === null && <div className="muted">Loading…</div>}
        {worlds?.length === 0 && <div className="muted">No worlds yet. Create one above.</div>}
        {worlds?.map((w) => (
          <div key={w.name} className="card world-card" onClick={() => !w.error && onOpen(w.name)}>
            <div className="row spread"><b style={{ fontSize: 16 }}>{w.name}</b>{w.running && <span className="chip pos"><span className="dot live" /> live</span>}</div>
            {w.error ? <div className="muted small">unreadable</div> : (
              <div className="muted small" style={{ marginTop: 6 }}>{w.residents} residents · {w.posts} posts · {w.communities} communities · period {w.period}</div>
            )}
          </div>
        ))}
      </div>
      <div className="features">
        {[["🧠", "Hybrid AI", "Rules decide when to act; a local LLM writes posts. No cloud, no cost."],
          ["🏘️", "Emergent communities", "Agents pick topics; popular ones become communities."],
          ["💭", "Emotions & opinions", "Moods react to the feed and to feedback on their posts."],
          ["🛡️", "Injection-proof", "Every post, including yours, is filtered. The LLM never chooses targets."]].map(([i, t, d]) => (
          <div key={t} className="card feature"><div className="i">{i}</div><b>{t}</b><div className="muted small">{d}</div></div>
        ))}
      </div>
    </div>
  );
}

type View = "feed" | "people" | "observatory";

function WorldApp({ world, onExit }: { world: string; onExit: () => void }) {
  const [state, setState] = useState<State | null>(null);
  const [err, setErr] = useState("");
  const [events, setEvents] = useState<EventT[]>([]);
  const [view, setView] = useState<View>(() => {
    const v = new URLSearchParams(location.search).get("view");
    return v === "people" || v === "observatory" ? v : "feed";
  });
  const [filter, setFilter] = useState<Filter>({ community: null, topic: null });
  const [profile, setProfile] = useState<number | null>(null);
  const [thread, setThread] = useState<number | null>(null);
  const [version, setVersion] = useState(0);
  const [freshIds, setFresh] = useState<Set<number>>(new Set());
  const { toast, show } = useToast();

  const loadState = useCallback(() => {
    api.state(world).then((s) => { setState(s); setErr(""); }).catch((e) => setErr((e as Error).message));
  }, [world]);
  useEffect(() => { loadState(); api.events(world).then(setEvents).catch(() => {}); }, [world, loadState]);

  const bumpState = useThrottle(loadState, 2500);
  const bumpViews = useThrottle(() => setVersion((v) => v + 1), 2000);
  const refresh = useCallback(() => { loadState(); setVersion((v) => v + 1); }, [loadState]);

  const connected = useLive(world, (m: LiveMsg) => {
    if (m.type === "event") {
      setEvents((ev) => [...ev.slice(-249), m.event]);
      if (m.post) {
        const id = m.post.id;
        setFresh((s) => new Set(s).add(id));
        setTimeout(() => setFresh((s) => { const n = new Set(s); n.delete(id); return n; }), 4000);
      }
      bumpViews(); bumpState();
    } else if (m.type === "period" || m.type === "status" || m.type === "residents") {
      loadState(); bumpViews();
    }
  });

  // Fallback polling while running, in case the socket drops.
  useEffect(() => {
    if (state?.run.status !== "running" || connected) return;
    const id = setInterval(refresh, 3000);
    return () => clearInterval(id);
  }, [state?.run.status, connected, refresh]);

  const ctx = useMemo(() => state && ({
    world, state, freshIds, refresh, toast: show,
    openProfile: (id: number) => setProfile(id), openThread: (id: number) => setThread(id),
  }), [world, state, freshIds, refresh, show]);

  if (err && !state) return (
    <div className="landing"><div className="error-banner">{err}</div><button className="btn" style={{ marginTop: 12 }} onClick={onExit}>← Back</button></div>
  );
  if (!ctx || !state) return <div className="landing muted">Opening {world}…</div>;

  return (
    <WorldCtx.Provider value={ctx}>
      <header className="topbar">
        <div className="brand" onClick={onExit} title="All worlds"><div className="brand-mark">◎</div>Socialverse</div>
        <span className="chip">{state.name}</span>
        <nav className="tabs">
          {([["feed", "Feed"], ["people", "People"], ["observatory", "Observatory"]] as const).map(([k, l]) => (
            <button key={k} className={`tab ${view === k ? "active" : ""}`} onClick={() => setView(k)}>{l}</button>
          ))}
        </nav>
        <div className="grow" />
        <span className="clock">{periodLabel(state.clock)} · period {state.period}</span>
        <span className={`chip ${state.run.status === "running" ? "pos" : ""}`} title={connected ? "Live connection" : "Reconnecting…"}>
          <span className={`dot ${state.run.status === "running" ? "live" : ""}`} style={{ background: connected ? undefined : "var(--warn)" }} />
          {state.run.status === "running" ? "Live" : "Paused"}
        </span>
        <span className="chip warn" title="Everything here is synthetic">SIMULATED</span>
        <ThemeToggle />
      </header>
      <div className={`layout ${view === "observatory" ? "wide" : ""}`}>
        <LeftSidebar filter={filter} setFilter={(f) => { setFilter(f); setView("feed"); }} />
        {view === "feed" && <Feed filter={filter} setFilter={setFilter} version={version} />}
        {view === "people" && <People />}
        {view === "observatory" && <Observatory version={version} />}
        {view !== "observatory" && (
          <aside className="sidebar right">
            <ControlPanel key={state.run.status} />
            <Activity events={events} />
            <MoodPanel />
          </aside>
        )}
      </div>
      {view === "observatory" && (
        <div style={{ maxWidth: 1400, margin: "0 auto", padding: "0 20px 20px" }}><ControlPanel key={state.run.status} /></div>
      )}
      {profile !== null && <ProfileDrawer id={profile} version={version} onClose={() => setProfile(null)} />}
      {thread !== null && <ThreadModal id={thread} version={version} onClose={() => setThread(null)} />}
      {toast && <div className={`toast ${toast.err ? "err" : ""}`}>{toast.msg}</div>}
    </WorldCtx.Provider>
  );
}

export default function App() {
  const [world, setWorld] = useState<string | null>(() => new URLSearchParams(location.search).get("world"));
  const open = (w: string | null) => {
    const url = w ? `?world=${encodeURIComponent(w)}` : location.pathname;
    history.pushState(null, "", url);
    setWorld(w);
  };
  useEffect(() => {
    const onPop = () => setWorld(new URLSearchParams(location.search).get("world"));
    addEventListener("popstate", onPop);
    return () => removeEventListener("popstate", onPop);
  }, []);
  return world ? <WorldApp key={world} world={world} onExit={() => open(null)} /> : <Landing onOpen={open} />;
}

import { createContext, useContext } from "react";
import type { State } from "./api";

export type Ctx = {
  world: string;
  state: State;
  openProfile: (id: number) => void;
  openThread: (id: number) => void;
  toast: (msg: string, err?: boolean) => void;
  refresh: () => void;
  freshIds: Set<number>;
};

export const WorldCtx = createContext<Ctx | null>(null);

export function useWorld(): Ctx {
  const c = useContext(WorldCtx);
  if (!c) throw new Error("WorldCtx missing");
  return c;
}

const PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"];

export function colorFor(key: string | number): string {
  const s = String(key);
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return PALETTE[h % PALETTE.length];
}

export const groupColor = (g: number) => PALETTE[((g % PALETTE.length) + PALETTE.length) % PALETTE.length];

export const EMOTION_COLORS: Record<string, string> = {
  happy: "#1baf7a", excited: "#eda100", calm: "#2a78d6", curious: "#6250d6", neutral: "#9a9a96",
  anxious: "#eb6834", sad: "#e87ba4", angry: "#e34948", human: "#52514e",
};

export const EMOTION_EMOJI: Record<string, string> = {
  happy: "🙂", excited: "🤩", calm: "😌", curious: "🤔", neutral: "😐", anxious: "😟", sad: "😢", angry: "😠",
};

export function initials(handle: string): string {
  const parts = handle.split(/[_\s]+/).filter(Boolean);
  return ((parts[0]?.[0] ?? "?") + (parts[1]?.[0] ?? "")).toUpperCase();
}

export function Avatar({ id, handle, emoji, size, human }: { id: number; handle: string; emoji?: string; size?: "sm" | "lg"; human?: boolean }) {
  const { openProfile } = useWorld();
  return (
    <div className={`avatar ${size ?? ""}`} title={handle}
      style={{ background: human ? "linear-gradient(135deg,#151826,#4d5468)" : colorFor(handle), fontSize: human && size !== "lg" ? 11 : undefined }}
      onClick={(e) => { e.stopPropagation(); openProfile(id); }}>
      {human ? (size === "sm" ? "Y" : "You") : initials(handle)}
      {emoji && <span className="emo">{emoji}</span>}
    </div>
  );
}

export function StanceChip({ value, reply }: { value: number | null; reply?: boolean }) {
  if (value === null || value === undefined) return null;
  if (reply) {
    if (value > 0.2) return <span className="chip pos">agrees</span>;
    if (value < -0.2) return <span className="chip neg">disagrees</span>;
    return <span className="chip">mixed</span>;
  }
  if (value > 0.25) return <span className="chip pos">positive</span>;
  if (value < -0.25) return <span className="chip neg">critical</span>;
  return null;
}

export const fmt = (v: number | null | undefined, d = 2) => (v === null || v === undefined || Number.isNaN(v) ? "–" : v.toFixed(d));

export function periodLabel(clock: number): string {
  const day = Math.floor(clock / (60 * 24)) + 1;
  const mins = clock % (60 * 24);
  const h = Math.floor(mins / 60);
  const m = Math.floor(mins % 60);
  return `Day ${day} · ${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

export const FORMAT_LABEL: Record<string, string> = {
  meme: "😂 meme", hot_take: "🌶️ hot take", gossip: "👀 gossip", news_share: "📰 news", question: "❓ question",
  joke: "🤡 joke", rant: "😤 rant", wholesome: "💛 wholesome", personal_update: "☕ life update", hype: "🔥 hype",
  reaction: "💬 reaction",
};

export const ARCHETYPE_ICON: Record<string, string> = {
  "meme lord": "😂", gossip: "👀", "news junkie": "📰", influencer: "✨", "hot-take artist": "🌶️",
  wholesome: "💛", "local regular": "🏡", "sports fan": "🫎", lurker: "🫥",
};

/** Text memes use " / " between lines; render them as a meme card. Plain text otherwise. */
export function PostBody({ text, format, small }: { text: string; format: string; small?: boolean }) {
  if (format === "meme" && text.includes(" / ")) {
    return (
      <div className="meme">
        {text.split(" / ").map((line, i) => <div key={i} className="meme-line">{line}</div>)}
      </div>
    );
  }
  return <div className="post-body" style={small ? { fontSize: 14, margin: "2px 0 4px" } : undefined}>{text}</div>;
}

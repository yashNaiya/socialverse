// Typed client for the Socialverse API. All agent text arrives as plain data and
// is rendered by React as text nodes (never as HTML), so it can't inject markup.

export type Emotion = { valence: number; arousal: number; label: string; emoji: string };

export type PostT = {
  id: number; author: number; handle: string; human: boolean; parent: number | null;
  community: number | null; community_name: string | null; period: number; text: string;
  stance: number; topic: string; agreement: number | null; score: number; replies: number;
  emotion: string; children?: PostT[]; format: string;
  happening: { id: number; category: string; icon: string; headline: string } | null;
};

export type Happening = { id: number; category: string; icon: string; headline: string; topic: string; sentiment: number;
  controversy: number; heat: number; period: number; posts?: number; active?: boolean; threads?: number; replies?: number;
  authors?: number; reach?: number; peak_period?: number | null };

export type Community = {
  id: number; name: string; topic: string; founder: number; founder_handle: string;
  founded_period: number; members: number; posts: number; mood: number;
  mean_opinion: number | null; joined: boolean;
};

export type AgentLite = {
  id: number; handle: string; job: string; kind: "agent" | "human"; emotion: Emotion | null;
  posts: number; followers: number; following: number; communities: number[]; joined_period: number; archetype: string | null;
};

export type Metrics = Record<string, any> & { period: number; policy: string; mode: string; brain: string };

export type RunInfo = {
  status: "running" | "paused" | "error"; error: string; speed: number;
  target_period: number | null; llm: { calls: number; errors: number; avg_seconds: number };
};

export type State = {
  name: string; period: number; clock: number; policy: string; mode: string; brain: string;
  human_id: number; label: string;
  totals: { residents: number; posts: number; replies: number; votes: number; follows: number; topics: number; communities: number };
  counters: { moderated: number; quarantined: number; rejected: number };
  trending: { topic: string; posts: number; score: number; community: number | null; happening: PostT["happening"] }[];
  happenings: Happening[];
  communities: Community[]; agents: AgentLite[]; mood: Record<string, number>;
  latest_metrics: Metrics | null; round_progress: number; run: RunInfo;
};

export type EventT = {
  period: number; clock: number; agent: number; handle: string; action: string; target: number;
  ok: boolean; note: string; post_id: number | null; topic: string; stance_change: number;
  emotion: string; human: boolean; category?: string;
};

export type Profile = {
  id: number; handle: string; job: string; kind: string; archetype: string | null; voice: string; interests: string[]; values: string[];
  style: string; openness: number; activity: number; joined_period: number; emotion: Emotion | null;
  opinions: { topic: string; value: number }[]; memory: string[];
  communities: { id: number; name: string }[];
  followers: { id: number; handle: string }[]; following: { id: number; handle: string }[];
  followed_by_you: boolean; stats: { posts: number; followers: number; following: number };
  recent_posts: PostT[];
};

export type WorldInfo = { name: string; residents: number; posts: number; communities: number; period: number; running: boolean; version: number; error?: string };
export type NetworkT = { nodes: { id: number; handle: string; kind: string; x: number; y: number; group: number; pagerank: number; degree: number; emotion: string }[]; edges: { s: number; t: number; w: number }[] };
export type TopicsT = { table: { topic: string; threads: number; replies: number; participants: number; first_round: number; mean_opinion: number; polarization: number }[]; timeline: Record<string, number>[] };

export class ApiError extends Error {}

async function req<T>(path: string, body?: unknown): Promise<T> {
  const init: RequestInit = body === undefined
    ? { method: "GET" }
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, init);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const d = (data as any).detail;
    throw new ApiError(typeof d === "string" ? d : Array.isArray(d) ? "Invalid input" : `Request failed (${r.status})`);
  }
  return data as T;
}

const w = (name: string) => `/api/worlds/${encodeURIComponent(name)}`;

export const api = {
  worlds: () => req<WorldInfo[]>("/api/worlds"),
  create: (name: string, residents: number, seed: number) => req<State>("/api/worlds", { name, residents, seed }),
  state: (name: string) => req<State>(`${w(name)}/state`),
  feed: (name: string, q: { sort: string; community?: number | null; topic?: string | null; author?: number | null; limit?: number }) => {
    const p = new URLSearchParams({ sort: q.sort, limit: String(q.limit ?? 30) });
    if (q.community !== undefined && q.community !== null) p.set("community", String(q.community));
    if (q.topic) p.set("topic", q.topic);
    if (q.author !== undefined && q.author !== null) p.set("author", String(q.author));
    return req<{ total: number; posts: PostT[] }>(`${w(name)}/feed?${p}`);
  },
  thread: (name: string, id: number) => req<PostT>(`${w(name)}/posts/${id}`),
  agent: (name: string, id: number) => req<Profile>(`${w(name)}/agents/${id}`),
  events: (name: string) => req<EventT[]>(`${w(name)}/events?limit=120`),
  metrics: (name: string) => req<Metrics[]>(`${w(name)}/metrics`),
  network: (name: string) => req<NetworkT>(`${w(name)}/network`),
  happenings: (name: string) => req<Happening[]>(`${w(name)}/happenings`),
  topics: (name: string) => req<TopicsT>(`${w(name)}/topics`),
  run: (name: string, s: { brain: string; mode: string; policy: string; speed: number; periods: number | null }) => req<State>(`${w(name)}/run`, s),
  pause: (name: string) => req<{ ok: boolean }>(`${w(name)}/pause`, {}),
  addResidents: (name: string, count: number) => req<State>(`${w(name)}/residents`, { count }),
  post: (name: string, text: string, topic: string, community: number | null, stance: number) =>
    req<{ post: PostT }>(`${w(name)}/human/post`, { text, topic, community, stance }),
  reply: (name: string, post_id: number, text: string, agreement: number) => req<{ post: PostT }>(`${w(name)}/human/reply`, { post_id, text, agreement }),
  vote: (name: string, post_id: number, value: 1 | -1) => req<{ score: number }>(`${w(name)}/human/vote`, { post_id, value }),
  follow: (name: string, agent_id: number) => req(`${w(name)}/human/follow`, { agent_id }),
  join: (name: string, community_id: number) => req(`${w(name)}/human/join`, { community_id }),
};

export function wsUrl(name: string): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/ws/worlds/${encodeURIComponent(name)}`;
}

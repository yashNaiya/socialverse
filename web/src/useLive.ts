import { useEffect, useRef, useState } from "react";
import { wsUrl, type EventT, type Metrics, type PostT, type RunInfo } from "./api";

export type LiveMsg =
  | { type: "event"; event: EventT; post: PostT | null }
  | { type: "period"; metrics: Metrics }
  | { type: "status"; run: RunInfo }
  | { type: "residents"; count: number }
  | { type: "ping" };

/** Subscribes to a world's live stream; reconnects with backoff. */
export function useLive(world: string, onMsg: (m: LiveMsg) => void): boolean {
  const [connected, setConnected] = useState(false);
  const handler = useRef(onMsg);
  handler.current = onMsg;

  useEffect(() => {
    let ws: WebSocket | null = null;
    let closed = false;
    let retry = 500;
    let timer: number | undefined;

    const connect = () => {
      ws = new WebSocket(wsUrl(world));
      ws.onopen = () => { setConnected(true); retry = 500; };
      ws.onmessage = (e) => {
        try { handler.current(JSON.parse(e.data) as LiveMsg); } catch { /* ignore malformed frames */ }
      };
      ws.onclose = () => {
        setConnected(false);
        if (!closed) timer = window.setTimeout(connect, (retry = Math.min(retry * 2, 8000)));
      };
    };
    connect();
    return () => { closed = true; window.clearTimeout(timer); ws?.close(); };
  }, [world]);

  return connected;
}

/** Calls fn at most once per `ms`, trailing edge included. */
export function useThrottle(fn: () => void, ms: number): () => void {
  const last = useRef(0);
  const pending = useRef<number | undefined>(undefined);
  const f = useRef(fn);
  f.current = fn;
  useEffect(() => () => window.clearTimeout(pending.current), []);
  return useRef(() => {
    const now = Date.now();
    const wait = last.current + ms - now;
    if (wait <= 0) { last.current = now; f.current(); return; }
    if (pending.current === undefined) {
      pending.current = window.setTimeout(() => {
        pending.current = undefined; last.current = Date.now(); f.current();
      }, wait);
    }
  }).current;
}

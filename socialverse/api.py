"""FastAPI server: REST + WebSocket for the React frontend.

Security controls (local single-user app):
* Bound to loopback only (enforced in __main__).
* Host header allow-list: blocks DNS-rebinding attacks from malicious websites.
* Origin check on every state-changing request AND on WebSocket upgrades: a
  website you visit can't drive your simulation or read its live stream.
* Mutating requests must be application/json (no "simple" cross-site form posts).
* Request bodies capped at 8 KB; every field length/range-validated by Pydantic.
* Human actions are rate-limited and pass the SAME sanitize / moderation /
  prompt-injection quarantine as agent posts.
* Only free brains (mock / local Ollama) can be started from the UI.
* Generic error messages; agent text is returned as data and rendered as
  plain text by React (never as HTML).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from echo_sim import config

from . import store, views
from .runner import BRAINS, MODES, Manager
from .world import HumanError

log = logging.getLogger("socialverse.api")

PORT = int(os.getenv("SOCIALVERSE_PORT", "8000"))
DEV_PORT = 5173
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "testserver"]
MAX_BODY_BYTES = 8 * 1024
WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"


# ------------------------------------------------------------------ live hub
class Hub:
    """Fan-out of world events from simulation threads to WebSocket clients."""

    def __init__(self):
        self.loop: asyncio.AbstractEventLoop | None = None
        self.subs: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self.lock = threading.Lock()

    def publisher(self, name: str):
        def publish(msg: dict) -> None:
            loop = self.loop
            if loop is None or loop.is_closed():
                return
            with self.lock:
                queues = list(self.subs.get(name, ()))
            for q in queues:
                loop.call_soon_threadsafe(self._offer, q, msg)
        return publish

    @staticmethod
    def _offer(q: asyncio.Queue, msg: dict) -> None:
        if q.full():
            try:
                q.get_nowait()          # drop the oldest; the UI also polls
            except asyncio.QueueEmpty:
                pass
        q.put_nowait(msg)

    def subscribe(self, name: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=300)
        with self.lock:
            self.subs[name].add(q)
        return q

    def unsubscribe(self, name: str, q: asyncio.Queue) -> None:
        with self.lock:
            self.subs[name].discard(q)


class RateLimiter:
    def __init__(self, per_minute: int, burst_seconds: float):
        self.per_minute, self.burst = per_minute, burst_seconds
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self.lock:
            h = self.hits[key]
            while h and now - h[0] > 60:
                h.popleft()
            if len(h) >= self.per_minute or (h and now - h[-1] < self.burst):
                raise HTTPException(429, "Slow down a little: you're posting faster than the platform allows.")
            h.append(now)


# ------------------------------------------------------------------ schemas
Name = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")


class CreateWorld(BaseModel):
    name: str = Name
    residents: int = Field(default=30, ge=2, le=config.MAX_AGENTS)
    seed: int = Field(default=42, ge=0, le=2**31)


class RunSettings(BaseModel):
    brain: Literal[BRAINS] = "mock"  # type: ignore[valid-type]
    mode: Literal[MODES] = "continuous"  # type: ignore[valid-type]
    policy: Literal["chronological", "engagement", "personalized"] = "personalized"
    speed: float = Field(default=1.0, ge=0, le=5)
    periods: int | None = Field(default=None, ge=1, le=500)


class Residents(BaseModel):
    count: int = Field(ge=1, le=config.MAX_AGENTS)


class HumanPost(BaseModel):
    text: str = Field(min_length=2, max_length=config.MAX_POST_CHARS)
    topic: str = Field(default="", max_length=40)
    community: int | None = Field(default=None, ge=1)
    stance: float = Field(default=0.0, ge=-1, le=1)


class HumanReply(BaseModel):
    post_id: int = Field(ge=1)
    text: str = Field(min_length=2, max_length=config.MAX_POST_CHARS)
    agreement: float = Field(default=0.0, ge=-1, le=1)


class HumanVote(BaseModel):
    post_id: int = Field(ge=1)
    value: Literal[1, -1]


class HumanFollow(BaseModel):
    agent_id: int = Field(ge=0)


class HumanJoin(BaseModel):
    community_id: int = Field(ge=1)


# ------------------------------------------------------------------ app
def origins_for(port: int) -> set[str]:
    return {f"http://{h}:{p}" for h in ("127.0.0.1", "localhost") for p in (port, DEV_PORT)}


def create_app(root: Path = store.WORLDS_DIR, serve_web: bool = True, port: int = PORT) -> FastAPI:
    ALLOWED_ORIGINS = origins_for(port)
    hub = Hub()
    manager = Manager(root, publish_factory=hub.publisher)
    writes = RateLimiter(per_minute=20, burst_seconds=2.0)

    @asynccontextmanager
    async def lifespan(app):
        hub.loop = asyncio.get_running_loop()
        yield
        await asyncio.to_thread(manager.shutdown)

    app = FastAPI(title="Socialverse", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.manager = manager
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin is not None and origin not in ALLOWED_ORIGINS:
                return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
            ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return JSONResponse({"detail": "content-type must be application/json"}, status_code=415)
            try:
                length = int(request.headers.get("content-length", "0"))
            except ValueError:
                length = MAX_BODY_BYTES + 1
            if length > MAX_BODY_BYTES:
                return JSONResponse({"detail": "request too large"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; connect-src 'self' ws://127.0.0.1:* ws://localhost:*; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; "
            "frame-ancestors 'none'")
        return response

    def runner(name: str):
        try:
            return manager.get(name)
        except store.WorldError as e:
            raise HTTPException(404 if "does not exist" in str(e) else 409, str(e)) from e

    def state(name: str) -> dict:
        r = runner(name)
        with r.world.lock:
            s = views.summary(r.world)
        s["run"] = r.info()
        return s

    # ---------------- worlds
    @app.get("/api/worlds")
    def list_worlds():
        running = {n for n, r in manager.runners.items() if r.running}
        return [{**w, "running": w["name"] in running} for w in store.list_worlds(manager.root)]

    @app.post("/api/worlds", status_code=201)
    def create_world(body: CreateWorld):
        try:
            manager.create(body.name, body.residents, body.seed)
        except store.WorldError as e:
            raise HTTPException(409, str(e)) from e
        return state(body.name)

    @app.get("/api/worlds/{name}/state")
    def get_state(name: str):
        return state(name)

    @app.post("/api/worlds/{name}/run")
    def run(name: str, body: RunSettings):
        try:
            manager.start(name, **body.model_dump())
        except store.WorldError as e:
            raise HTTPException(409, str(e)) from e
        except RuntimeError as e:          # Ollama not ready / already running
            raise HTTPException(409, str(e)) from e
        return state(name)

    @app.post("/api/worlds/{name}/pause")
    def pause(name: str):
        runner(name).pause(wait=0)
        return {"ok": True}

    @app.post("/api/worlds/{name}/residents")
    def add_residents(name: str, body: Residents):
        try:
            runner(name).world.add_residents(body.count)
        except HumanError as e:
            raise HTTPException(400, str(e)) from e
        return state(name)

    # ---------------- reading
    @app.get("/api/worlds/{name}/feed")
    def get_feed(name: str, sort: Literal["hot", "new", "top"] = "hot",
                 community: int | None = Query(default=None, ge=-1),
                 topic: str | None = Query(default=None, max_length=40),
                 author: int | None = Query(default=None, ge=0),
                 limit: int = Query(default=25, ge=1, le=100), offset: int = Query(default=0, ge=0, le=100000)):
        w = runner(name).world
        with w.lock:
            return views.feed(w, sort, community, topic, author, limit, offset)

    @app.get("/api/worlds/{name}/posts/{post_id}")
    def get_thread(name: str, post_id: int):
        w = runner(name).world
        with w.lock:
            t = views.thread(w, post_id)
        if t is None:
            raise HTTPException(404, "post not found")
        return t

    @app.get("/api/worlds/{name}/agents/{agent_id}")
    def get_agent(name: str, agent_id: int):
        w = runner(name).world
        with w.lock:
            a = views.agent(w, agent_id)
        if a is None:
            raise HTTPException(404, "resident not found")
        return a

    @app.get("/api/worlds/{name}/events")
    def get_events(name: str, limit: int = Query(default=80, ge=1, le=250)):
        w = runner(name).world
        with w.lock:
            return list(w.recent_events)[-limit:]

    @app.get("/api/worlds/{name}/metrics")
    def get_metrics(name: str):
        w = runner(name).world
        with w.lock:
            return list(w.metrics)

    @app.get("/api/worlds/{name}/network")
    def get_network(name: str):
        w = runner(name).world
        with w.lock:
            return views.network(w)

    @app.get("/api/worlds/{name}/happenings")
    def get_happenings(name: str):
        w = runner(name).world
        with w.lock:
            return views.happenings(w)

    @app.get("/api/worlds/{name}/topics")
    def get_topics(name: str):
        w = runner(name).world
        with w.lock:
            return views.topics(w)

    # ---------------- human participant
    def human(name: str, fn, *args):
        writes.check(name)
        w = runner(name).world
        try:
            return fn(w, *args)
        except HumanError as e:
            raise HTTPException(400, str(e)) from e
        except store.WorldError as e:
            raise HTTPException(409, str(e)) from e

    @app.post("/api/worlds/{name}/human/post", status_code=201)
    def human_post(name: str, body: HumanPost):
        return human(name, lambda w: w.human_post(body.text, body.topic, body.community, body.stance))

    @app.post("/api/worlds/{name}/human/reply", status_code=201)
    def human_reply(name: str, body: HumanReply):
        return human(name, lambda w: w.human_reply(body.post_id, body.text, body.agreement))

    @app.post("/api/worlds/{name}/human/vote")
    def human_vote(name: str, body: HumanVote):
        return human(name, lambda w: w.human_vote(body.post_id, body.value))

    @app.post("/api/worlds/{name}/human/follow")
    def human_follow(name: str, body: HumanFollow):
        return human(name, lambda w: w.human_follow(body.agent_id))

    @app.post("/api/worlds/{name}/human/join")
    def human_join(name: str, body: HumanJoin):
        return human(name, lambda w: w.human_join(body.community_id))

    # ---------------- live stream
    @app.websocket("/ws/worlds/{name}")
    async def live(ws: WebSocket, name: str):
        origin = ws.headers.get("origin")
        host = (ws.headers.get("host") or "").split(":")[0]
        if origin not in ALLOWED_ORIGINS or host not in ALLOWED_HOSTS:
            await ws.close(code=1008)          # policy violation: cross-site WebSocket hijacking attempt
            return
        try:
            store.validate_name(name)
            await asyncio.to_thread(runner, name)
        except (store.WorldError, HTTPException):
            await ws.close(code=1008)
            return
        await ws.accept()
        q = hub.subscribe(name)
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=20)
                except asyncio.TimeoutError:
                    msg = {"type": "ping"}
                await ws.send_json(msg)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            hub.unsubscribe(name, q)

    # ---------------- React build
    if serve_web and WEB_DIST.exists():
        app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith(("api/", "ws/")):
                raise HTTPException(404)
            return FileResponse(WEB_DIST / "index.html")

    return app

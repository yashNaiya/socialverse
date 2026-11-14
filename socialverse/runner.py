"""Runs worlds in a background thread and manages which worlds are open.

The server process is the single writer for every world it opens (it holds the
SQLite lease). Only one world simulates at a time, so the shared local LLM is
never oversubscribed.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from echo_sim import safety

from . import store
from .feed import POLICIES
from .hybrid import make_writer
from .world import World

log = logging.getLogger("socialverse.runner")

MODES = ("continuous", "rounds")
BRAINS = ("mock", "ollama")


class Runner:
    def __init__(self, world: World):
        self.world = world
        self.thread: threading.Thread | None = None
        self.stop_evt = threading.Event()
        self.writer = None
        self.status = "paused"
        self.error = ""
        self.speed = 1.0               # seconds of real time between agent actions
        self.target_period: int | None = None
        self.started_at = 0.0

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, brain: str, mode: str, policy: str, speed: float, periods: int | None) -> None:
        if self.running:
            raise RuntimeError("already running")
        if brain not in BRAINS or mode not in MODES or policy not in POLICIES:
            raise ValueError("invalid run settings")
        self.writer = make_writer(brain)            # raises if Ollama isn't ready
        self.speed = max(0.0, min(float(speed), 5.0))
        self.world.save_settings(brain=brain, mode=mode, policy=policy, status="running")
        with self.world.lock:
            self.target_period = self.world.period + periods if periods else None
        self.error = ""
        self.stop_evt.clear()
        self.status = "running"
        self.started_at = time.time()
        self.thread = threading.Thread(target=self._loop, name=f"world-{self.world.name}", daemon=True)
        self.thread.start()
        self._announce()

    def pause(self, wait: float = 0.0) -> None:
        self.stop_evt.set()
        if wait and self.thread:
            self.thread.join(timeout=wait)

    def _loop(self) -> None:
        w = self.world
        try:
            while not self.stop_evt.is_set():
                if self.target_period is not None and w.period >= self.target_period:
                    break
                ev = w.step_round(self.writer) if w.mode == "rounds" else w.step_continuous(self.writer)
                if ev and self.speed:
                    self.stop_evt.wait(self.speed)
            self.status = "paused"
        except safety.BudgetExceeded as e:
            self.status, self.error = "paused", str(e)
        except Exception as e:  # noqa: BLE001 - surface any failure to the UI, never crash the server
            log.exception("world %s stopped with an error", w.name)
            self.status, self.error = "error", f"{type(e).__name__}: {str(e)[:200]}"
        finally:
            try:
                if not w.closed:
                    w.save_settings(status=self.status)
            except Exception:  # noqa: BLE001
                log.exception("could not save status")
            self._announce()

    def info(self) -> dict:
        stats = self.writer.stats if self.writer else {"calls": 0, "errors": 0, "avg_seconds": 0.0}
        return {"status": "running" if self.running else self.status, "error": self.error,
                "speed": self.speed, "target_period": self.target_period, "llm": dict(stats)}

    def _announce(self) -> None:
        self.world.publish({"type": "status", "run": self.info()})


class Manager:
    def __init__(self, root: Path = store.WORLDS_DIR, publish_factory=None):
        self.root = Path(root)
        self.runners: dict[str, Runner] = {}
        self.lock = threading.Lock()
        self.publish_factory = publish_factory or (lambda name: (lambda msg: None))

    def get(self, name: str) -> Runner:
        store.validate_name(name)
        with self.lock:
            r = self.runners.get(name)
            if r is None:
                r = Runner(World(name, self.root, publish=self.publish_factory(name)))
                self.runners[name] = r
            return r

    def create(self, name: str, residents: int, seed: int) -> None:
        store.create_world(name, residents, seed, self.root)

    def start(self, name: str, **settings) -> Runner:
        with self.lock:
            busy = [n for n, r in self.runners.items() if r.running and n != name]
        if busy:
            raise RuntimeError(f"world '{busy[0]}' is already running; pause it first")
        r = self.get(name)
        r.start(**settings)
        return r

    def shutdown(self) -> None:
        with self.lock:
            runners = list(self.runners.values())
            self.runners.clear()
        for r in runners:
            r.pause(wait=10)
        for r in runners:
            try:
                r.world.close()
            except Exception:  # noqa: BLE001
                log.exception("closing world failed")

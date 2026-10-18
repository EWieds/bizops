"""Event-driven wake scheduler: an asyncio task in the serve process.

An agent is due when it is active, its org is active and the platform isn't paused, AND it has a
pending wake (new message or task) or its timer fired, AND its min interval has passed.
No agent runs just because a loop exists. A global semaphore caps concurrent runs.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from .core import Core
from .engine import expire_messages, platform_paused
from .runtime import run_agent
from .util import iso, now, parse_iso

log = logging.getLogger("bizops.scheduler")


class Scheduler:
    def __init__(self, core: Core, *, tick_s: float = 0.5):
        self.core = core
        self.tick_s = tick_s
        limit = int(core.settings.policy["runs"]["max_concurrent_runs"])
        self.sem = asyncio.Semaphore(limit)
        self.running: dict[str, asyncio.Task] = {}
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._last_sweep = 0.0

    def poke(self) -> None:
        self._wake.set()

    def due_agents(self) -> list[tuple[str, str]]:
        """[(agent_id, reason)] for agents that should run now."""
        db = self.core.db
        if platform_paused(db):
            return []
        ts = iso()
        rows = db.all(
            "SELECT a.id, a.wake_requested_at, a.next_wake_at, a.last_run_at, a.min_interval_s FROM agents a "
            "JOIN orgs o ON o.id = a.org_id WHERE a.status = 'active' AND o.status IN ('active','trial') "
            "AND (a.wake_requested_at IS NOT NULL OR (a.next_wake_at IS NOT NULL AND a.next_wake_at <= ?)) "
            "ORDER BY COALESCE(a.wake_requested_at, a.next_wake_at)", (ts,))
        out = []
        current = now()
        for r in rows:
            if r["id"] in self.running:
                continue
            last = parse_iso(r["last_run_at"])
            if last and current - last < timedelta(seconds=r["min_interval_s"] or 0):
                continue
            out.append((r["id"], "message" if r["wake_requested_at"] else "timer"))
        return out

    async def tick(self) -> int:
        loop = asyncio.get_running_loop()
        if loop.time() - self._last_sweep > 30:
            expire_messages(self.core.db)
            self._last_sweep = loop.time()
        started = 0
        for agent_id, reason in self.due_agents():
            self.running[agent_id] = asyncio.create_task(self._run(agent_id, reason))
            started += 1
        return started

    async def _run(self, agent_id: str, reason: str) -> None:
        try:
            async with self.sem:
                await run_agent(self.core, agent_id, reason)
        except Exception:
            log.exception("run failed for %s", agent_id)
        finally:
            self.running.pop(agent_id, None)
            self.poke()

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.tick_s)
            except TimeoutError:
                pass

    async def drain(self, timeout: float = 30.0) -> None:
        """Run until nothing is due and nothing is running (tests and `bizops tick`)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            await self.tick()
            if not self.running:
                if self.due_agents():
                    continue
                pending = self.core.db.scalar(
                    "SELECT COUNT(*) AS n FROM agents a JOIN orgs o ON o.id = a.org_id WHERE a.status = 'active' "
                    "AND o.status IN ('active','trial') AND a.wake_requested_at IS NOT NULL")
                if not pending or platform_paused(self.core.db):
                    return
                await asyncio.sleep(0.2)  # a wake is pending behind a min interval
            else:
                await asyncio.wait(list(self.running.values()), timeout=1.0)

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self.running:
            await asyncio.wait(list(self.running.values()), timeout=10)

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
from .engine import get_agent, platform_paused
from .events import log_event
from .maintenance import heartbeat_needed, run_jobs
from .runtime import run_agent
from .util import iso, iso_in, now, parse_iso

log = logging.getLogger("bizops.scheduler")


class Scheduler:
    def __init__(self, core: Core, *, tick_s: float = 0.5):
        self.core = core
        self.tick_s = tick_s
        limit = int(core.settings.policy["runs"]["max_concurrent_runs"])
        self.sem = asyncio.Semaphore(limit)
        # The owner never waits behind routine work: runs that answer the owner use their own slots.
        self.owner_sem = asyncio.Semaphore(int(core.settings.policy["runs"].get("owner_lane_runs", 2)))
        self.running: dict[str, asyncio.Task] = {}
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._last_sweep = -1e9
        self._last_poll = -1e9
        self._poll_task: asyncio.Task | None = None

    def poke(self) -> None:
        self._wake.set()

    def due_agents(self) -> list[tuple[str, str]]:
        """[(agent_id, reason)] for agents that should run now."""
        db = self.core.db
        if platform_paused(db):
            return []
        ts = iso()
        from .engine import get_setting
        paused_until = get_setting(db, "llm_paused_until", "")
        if paused_until and paused_until > ts:
            return []  # model provider limit: everyone waits, then resumes on their own
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
            if (last and current - last < timedelta(seconds=r["min_interval_s"] or 0)
                    and not self.owner_waiting(r["id"])):
                continue
            out.append((r["id"], "message" if r["wake_requested_at"] else "timer"))
        return out

    def owner_waiting(self, agent_id: str) -> bool:
        return bool(self.core.db.one("SELECT 1 AS x FROM messages WHERE recipient_kind = 'agent' AND recipient_id = ? "
                                     "AND sender_kind = 'human' AND status = 'unread' LIMIT 1", (agent_id,)))

    async def _acquire(self, agent_id: str) -> asyncio.Semaphore:
        """A run slot. Waiting runs re-check every second, so one the owner messages meanwhile moves to the
        owner lane instead of queuing behind routine work."""
        while True:
            if self.owner_waiting(agent_id):
                await self.owner_sem.acquire()
                return self.owner_sem
            try:
                await asyncio.wait_for(self.sem.acquire(), timeout=1.0)
                return self.sem
            except TimeoutError:
                continue

    def maintenance(self) -> None:
        loop = asyncio.get_running_loop()
        every = float(self.core.settings.policy.get("heartbeats", {}).get("maintenance_every_s", 30))
        if loop.time() - self._last_sweep >= every:
            self._last_sweep = loop.time()
            if not platform_paused(self.core.db):
                run_jobs(self.core.db, self.core.settings)

    async def skip_heartbeat(self, agent_id: str) -> bool:
        """Timer wake with nothing to act on: reschedule for free instead of running the model. For a
        routine review, ask Jev (or the cheapest model) whether a full work session is worth it."""
        db = self.core.db
        agent = get_agent(db, agent_id)
        if not agent:
            return True
        needed, why = heartbeat_needed(db, self.core.settings, agent)
        routing = self.core.settings.policy.get("routing", {})
        if needed and why == "daily review" and routing.get("triage_heartbeats", True) and self.core.llm.name != "dry-run":
            from .decide import charge
            from .runtime import preview_context
            try:
                d = await self.core.decider.noul(
                    preview_context(self.core, agent),
                    f"This is the scheduled review for a {agent['role']}. Is there anything that needs a full work "
                    "session now (a decision, a stuck task, a goal at risk, a deliverable to produce)?")
                charge(db, agent["org_id"], d, "heartbeat triage")
                if d.value < 0.3 and d.confidence >= float(routing.get("min_confidence", 0.75)) - 0.35:
                    needed, why = False, f"triage: nothing worth a session (p={d.value:.2f}, {d.source})"
            except Exception:
                pass
        if needed:
            return False
        db.execute("UPDATE agents SET next_wake_at = ?, last_heartbeat_skip_at = ? WHERE id = ?",
                   (iso_in(agent["wake_interval_s"] or 3600), iso(), agent_id))
        log_event(db, actor_kind="scheduler", kind="wake", action="heartbeat.skip", org_id=agent["org_id"],
                  agent_id=agent_id, result={"why": why}, outcome="ok")
        return True

    def poll(self) -> None:
        loop = asyncio.get_running_loop()
        every = float(self.core.settings.policy.get("heartbeats", {}).get("connector_poll_s", 60))
        busy = self._poll_task is not None and not self._poll_task.done()
        if not busy and loop.time() - self._last_poll >= every and not platform_paused(self.core.db):
            self._last_poll = loop.time()
            from .maintenance import poll_connectors
            self._poll_task = asyncio.create_task(poll_connectors(self.core))

    async def inbound(self) -> None:
        loop = asyncio.get_running_loop()
        every = float(self.core.settings.policy.get("heartbeats", {}).get("inbound_every_s", 15))
        if loop.time() - getattr(self, "_last_inbound", -1e9) >= every and not platform_paused(self.core.db):
            self._last_inbound = loop.time()
            from .business import process_inbound_smart
            try:
                await process_inbound_smart(self.core)
            except Exception:
                log.exception("inbound triage failed")

    async def tick(self) -> int:
        self.maintenance()
        await self.inbound()
        self.poll()
        started = 0
        for agent_id, reason in self.due_agents():
            if reason == "timer":
                if await self.skip_heartbeat(agent_id):
                    continue
                reason = "heartbeat"
            self.running[agent_id] = asyncio.create_task(self._run(agent_id, reason))
            started += 1
        return started

    async def _run(self, agent_id: str, reason: str) -> None:
        try:
            sem = await self._acquire(agent_id)
            try:
                await run_agent(self.core, agent_id, reason)
            finally:
                sem.release()
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
                pending = (pending or 0) + int(self.core.db.scalar(
                    "SELECT (SELECT COUNT(*) FROM sim_actions WHERE done = 0) + "
                    "(SELECT COUNT(*) FROM emails WHERE direction = 'in' AND processed = 0) AS n") or 0)
                from .engine import get_setting
                if not pending or platform_paused(self.core.db) or get_setting(self.core.db, "llm_paused_until", "") > iso():
                    return
                await asyncio.sleep(0.2)  # a wake is pending behind a min interval
            else:
                await asyncio.wait(list(self.running.values()), timeout=1.0)

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self.running:
            await asyncio.wait(list(self.running.values()), timeout=10)

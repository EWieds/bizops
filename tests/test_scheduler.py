import asyncio

from bizops.engine import human_message, set_paused
from bizops.scheduler import Scheduler
from bizops.util import iso_in


def test_nothing_due_without_events(core, org):
    core.db.execute("UPDATE agents SET next_wake_at = NULL, wake_requested_at = NULL")
    assert Scheduler(core).due_agents() == []


def test_message_makes_agent_due(core, org):
    human_message(core.db, core.settings, org["ceo"]["id"], "hello")
    assert (org["ceo"]["id"], "message") in Scheduler(core).due_agents()


def test_min_interval_respected(core, org):
    core.db.execute("UPDATE agents SET min_interval_s = 3600, last_run_at = ? WHERE id = ?", (iso_in(-10), org["ceo"]["id"]))
    human_message(core.db, core.settings, org["ceo"]["id"], "hello")
    assert org["ceo"]["id"] not in [a for a, _ in Scheduler(core).due_agents()]


def test_timer_wake(core, org):
    core.db.execute("UPDATE agents SET next_wake_at = ? WHERE id = ?", (iso_in(-1), org["cfo"]["id"]))
    assert (org["cfo"]["id"], "timer") in Scheduler(core).due_agents()


def test_paused_skipped(core, org):
    human_message(core.db, core.settings, org["ceo"]["id"], "hello")
    set_paused(core.db, "org", org["org"], True)
    assert org["ceo"]["id"] not in [a for a, _ in Scheduler(core).due_agents()]
    set_paused(core.db, "org", org["org"], False)
    set_paused(core.db, "platform", None, True)
    assert Scheduler(core).due_agents() == []


async def test_concurrency_cap(core, org, monkeypatch):
    core.settings.policy["runs"]["max_concurrent_runs"] = 2
    active, peak = 0, 0

    async def fake_run(c, agent_id, reason):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        c.db.execute("UPDATE agents SET wake_requested_at = NULL WHERE id = ?", (agent_id,))
        active -= 1
    monkeypatch.setattr("bizops.scheduler.run_agent", fake_run)
    for role in ("ceo", "coo", "cfo", "researcher", "marketer"):
        human_message(core.db, core.settings, org[role]["id"], "hi")
    await Scheduler(core).drain(timeout=10)
    assert peak == 2

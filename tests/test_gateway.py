import pytest

from bizops import ledger
from bizops.engine import set_paused
from bizops.gateway import invoke
from bizops.tools import REGISTRY, Ctx, tool


def ctx(core, agent):
    return Ctx(core=core, agent=agent, run_id=None)


def events(core, call_id):
    return core.db.all("SELECT phase, outcome FROM events WHERE call_id = ? ORDER BY id", (call_id,))


async def test_writes_before_and_after(core, org):
    r = await invoke(ctx(core, org["ceo"]), "note_write", {"key": "k", "body": "b", "reason": "test"})
    assert r.ok
    assert events(core, r.call_id) == [{"phase": "before", "outcome": None}, {"phase": "after", "outcome": "ok"}]
    assert core.db.scalar("SELECT reason FROM events WHERE call_id = ? AND phase = 'before'", (r.call_id,)) == "test"


async def test_ungranted_tool_refused_and_logged(core, org):
    r = await invoke(ctx(core, org["writer"]), "web_search", {"query": "x"})  # writer has no web grant
    assert not r.ok and "grant" in r.content
    assert events(core, r.call_id) == [{"phase": "point", "outcome": "refused"}]
    assert core.db.scalar("SELECT COUNT(*) AS n FROM policy_decisions WHERE rule = 'grant'") == 1


async def test_unknown_tool(core, org):
    r = await invoke(ctx(core, org["ceo"]), "rm_rf", {})
    assert not r.ok and r.outcome == "refused"


@pytest.mark.parametrize("target", ["agent", "org", "platform"])
async def test_kill_switches(core, org, target):
    tid = {"agent": org["ceo"]["id"], "org": org["org"], "platform": None}[target]
    set_paused(core.db, target, tid, True)
    r = await invoke(ctx(core, org["ceo"]), "note_write", {"key": "k", "body": "b"})
    assert not r.ok and "kill_switch" in r.content
    set_paused(core.db, target, tid, False)
    assert (await invoke(ctx(core, org["ceo"]), "note_write", {"key": "k", "body": "b"})).ok


async def test_handler_exception_is_logged_not_raised(core, org):
    @tool("boom_test", grant="core", action_class="internal", description="x")
    async def boom(c, a):
        raise RuntimeError("kaboom")
    try:
        r = await invoke(ctx(core, org["ceo"]), "boom_test", {})
        assert r.outcome == "error" and "kaboom" in r.content
        assert events(core, r.call_id)[-1]["outcome"] == "error"
    finally:
        REGISTRY.pop("boom_test")


async def test_hard_stop_queues_and_blocks_task(core, org):
    from bizops.engine import create_task
    t = create_task(core.db, creator=None, assignee_id=org["ceo"]["id"], title="Sign it", deliverable="signed")
    r = await invoke(ctx(core, org["ceo"]), "request_human",
                     {"category": "legal_commitment", "summary": "sign", "task_id": t["id"]})
    assert r.outcome == "hard_stop" and "continue" in r.content.lower()
    d = core.db.one("SELECT * FROM policy_decisions WHERE verdict = 'hard_stop'")
    assert d["status"] == "open" and d["task_id"] == t["id"]
    assert core.db.scalar("SELECT status FROM tasks WHERE id = ?", (t["id"],)) == "blocked"


async def test_rate_limit(core, org):
    core.settings.policy["rate_limits"]["note_read"] = 2
    c = ctx(core, org["ceo"])
    assert (await invoke(c, "note_read", {})).ok
    assert (await invoke(c, "note_read", {})).ok
    r = await invoke(c, "note_read", {})
    assert not r.ok and "rate_limit" in r.content


async def test_tool_spend_blocked_by_budget(core, org):
    core.db.execute("UPDATE agents SET budget_micros = 1000 WHERE id = ?", (org["researcher"]["id"],))
    r = await invoke(ctx(core, org["researcher"]), "web_search", {"query": "lawn care"})
    assert not r.ok and "agent.budget" in r.content
    assert ledger.spent(core.db, since="2000", agent_id=org["researcher"]["id"]) == 0


async def test_tool_spend_recorded(core, org):
    r = await invoke(ctx(core, org["researcher"]), "web_search", {"query": "lawn care"})
    assert r.ok and "untrusted_web_content" in r.content
    assert ledger.spent(core.db, since="2000", agent_id=org["researcher"]["id"]) == 10_000

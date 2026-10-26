import pytest

from bizops.engine import PolicyRefusal, get_agent, retire_agent, spawn_agent
from bizops.gateway import invoke
from bizops.goals import record_measurement, set_goal, update_goal
from bizops.tools import Ctx


async def test_set_goal_cascades_and_notifies(core, org):
    r = await invoke(Ctx(core=core, agent=org["ceo"]), "set_goal",
                     {"owner": org["ceo"]["id"], "title": "Leads", "metric": "qualified leads", "target": 20, "due": "30"})
    assert r.ok
    gid = core.db.scalar("SELECT id FROM goals")
    r = await invoke(Ctx(core=core, agent=org["ceo"]), "set_goal",
                     {"owner": org["coo"]["id"], "title": "Ship site", "metric": "pages live", "target": 5, "due": "14",
                      "parent_goal_id": gid})
    assert r.ok
    msg = core.db.one("SELECT * FROM messages WHERE recipient_id = ? AND sender_kind = 'system'", (org["coo"]["id"],))
    assert "gave you a goal" in msg["body"]


async def test_goal_scope_and_limits(core, org):
    r = await invoke(Ctx(core=core, agent=org["writer"]), "set_goal",
                     {"owner": org["ceo"]["id"], "title": "x", "metric": "m", "target": 1, "due": "3"})
    assert not r.ok  # writer has no goals grant
    for i in range(5):
        set_goal(core.db, core.settings, setter=None, owner_id=org["coo"]["id"], title=f"g{i}", metric="m", target=1)
    with pytest.raises(PolicyRefusal):
        set_goal(core.db, core.settings, setter=None, owner_id=org["coo"]["id"], title="g6", metric="m", target=1)


def test_measurement_owner_only_and_claims(core, org):
    g = set_goal(core.db, core.settings, setter=None, owner_id=org["ceo"]["id"], title="Rev", metric="usd", target=100)
    with pytest.raises(PermissionError):
        record_measurement(core.db, goal_id=g["id"], value=1, source="agent")
    out = update_goal(core.db, actor=org["ceo"], goal_id=g["id"], status="achieved", progress_note="done!")
    assert "without a measured value" in out["progress_note"]
    record_measurement(core.db, goal_id=g["id"], value=120, source="human")
    assert core.db.scalar("SELECT measured_value FROM goals WHERE id = ?", (g["id"],)) == 120


def test_spawn_allocates_from_hirer_budget(core, org):
    core.settings.policy["budgets"]["agent_budgets"] = True
    before = get_agent(core.db, org["coo"]["id"])["budget_micros"]
    row = spawn_agent(core.db, core.settings, hirer=org["coo"], role="coder", budget_usd=2, focus="landing page")
    assert row["parent_agent_id"] == org["coo"]["id"] and row["depth"] == org["coo"]["depth"] + 1
    assert "Senior Software Engineer" in row["title"] and "landing page" in row["system_prompt"]
    assert get_agent(core.db, org["coo"]["id"])["budget_micros"] == before - 2_000_000
    with pytest.raises(PolicyRefusal) as e:
        spawn_agent(core.db, core.settings, hirer=org["coo"], role="coder", budget_usd=100, focus="x")
    assert e.value.rule == "spawn.budget"


def test_spawn_never_grants_money_the_hirer_lacks(core, org):
    row = spawn_agent(core.db, core.settings, hirer=org["coo"], role="ceo", budget_usd=1, focus="x")
    assert "money" not in row["grants_json"]


def test_spawn_limits(core, org):
    core.db.execute("UPDATE orgs SET settings_json = ? WHERE id = ?",
                    ('{"max_agents": 7, "max_depth": 4, "max_reports": 6}', org["org"]))
    spawn_agent(core.db, core.settings, hirer=org["ceo"], role="designer", budget_usd=1, focus="x")
    with pytest.raises(PolicyRefusal) as e:
        spawn_agent(core.db, core.settings, hirer=org["ceo"], role="designer", budget_usd=1, focus="y")
    assert e.value.rule == "spawn.max_agents"
    with pytest.raises(PolicyRefusal) as e:
        spawn_agent(core.db, core.settings, hirer=org["writer"], role="designer", budget_usd=1, focus="y")
    assert e.value.rule in ("spawn.max_depth", "spawn.max_agents")


def test_retire_returns_budget_and_moves_work(core, org):
    from bizops.engine import create_task
    t = create_task(core.db, creator=None, assignee_id=org["writer"]["id"], title="copy", deliverable="d")
    mk = get_agent(core.db, org["marketer"]["id"])["budget_micros"]
    retire_agent(core.db, actor=org["ceo"], agent_id=org["writer"]["id"], reason="KPI flat")
    assert get_agent(core.db, org["writer"]["id"])["status"] == "retired"
    assert get_agent(core.db, org["marketer"]["id"])["budget_micros"] == mk + org["writer"]["budget_micros"]
    assert core.db.scalar("SELECT assignee_id FROM tasks WHERE id = ?", (t["id"],)) == org["marketer"]["id"]
    with pytest.raises(PolicyRefusal):
        retire_agent(core.db, actor=org["writer"], agent_id=org["ceo"]["id"], reason="coup")


async def test_spawn_tool_through_gateway(core, org):
    r = await invoke(Ctx(core=core, agent=org["ceo"]), "spawn_agent",
                     {"role": "sales", "budget_usd": 1, "focus": "find lead buyers"})
    assert r.ok and core.db.scalar("SELECT COUNT(*) AS n FROM agents WHERE role = 'sales'") == 1
    r = await invoke(Ctx(core=core, agent=org["researcher"]), "spawn_agent",
                     {"role": "sales", "budget_usd": 1, "focus": "x"})
    assert not r.ok and "grant" in r.content


async def test_tools_with_their_own_reason_field_keep_it(core, org):
    """The gateway's audit 'reason' must not swallow a tool's own 'reason' parameter."""
    from bizops.engine import spawn_agent
    hire = spawn_agent(core.db, core.settings, hirer=org["ceo"], role="designer", budget_usd=1, focus="x")
    r = await invoke(Ctx(core=core, agent=org["ceo"]), "retire_agent", {"agent_id": hire["id"], "reason": "KPI flat"})
    assert r.ok, r.content
    assert core.db.scalar("SELECT reason FROM events WHERE call_id = ? AND phase = 'before'", (r.call_id,)) == "KPI flat"

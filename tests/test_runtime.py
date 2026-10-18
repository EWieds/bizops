from bizops import ledger
from bizops.engine import human_message, set_paused
from bizops.runtime import run_agent


async def test_run_records_cost_and_yields(core, org):
    human_message(core.db, core.settings, org["ceo"]["id"], "Please research lawn care pricing in Columbus")
    rid = await run_agent(core, org["ceo"]["id"], "message")
    run = core.db.one("SELECT * FROM runs WHERE id = ?", (rid,))
    assert run["status"] == "yielded" and run["turns"] >= 2 and run["cost_micros"] > 0
    assert ledger.spent(core.db, since="2000", agent_id=org["ceo"]["id"]) > 0
    assert core.db.scalar("SELECT status FROM messages WHERE recipient_id = ?", (org["ceo"]["id"],)) == "read"
    assert core.db.scalar("SELECT COUNT(*) AS n FROM messages WHERE recipient_kind = 'human'") == 1


async def test_turn_limit(core, org):
    core.settings.policy["runs"]["max_turns"] = 1
    human_message(core.db, core.settings, org["ceo"]["id"], "Please research lawn care pricing in Columbus")
    rid = await run_agent(core, org["ceo"]["id"], "message")
    assert core.db.scalar("SELECT status FROM runs WHERE id = ?", (rid,)) == "turn_limit"


async def test_budget_stops_run_mid_way(core, org):
    core.db.execute("UPDATE agents SET budget_micros = 0 WHERE id = ?", (org["ceo"]["id"],))
    human_message(core.db, core.settings, org["ceo"]["id"], "hi")
    rid = await run_agent(core, org["ceo"]["id"], "message")
    run = core.db.one("SELECT * FROM runs WHERE id = ?", (rid,))
    assert run["status"] == "budget" and run["turns"] == 0
    assert core.db.scalar("SELECT COUNT(*) AS n FROM policy_decisions WHERE rule = 'agent.budget'") == 1


async def test_platform_daily_cap(core, org):
    core.settings.policy["platform"]["daily_cap_usd"] = 0
    human_message(core.db, core.settings, org["ceo"]["id"], "hi")
    rid = await run_agent(core, org["ceo"]["id"], "message")
    assert core.db.scalar("SELECT status FROM runs WHERE id = ?", (rid,)) == "budget"


async def test_paused_agent_does_not_run(core, org):
    set_paused(core.db, "agent", org["ceo"]["id"], True)
    assert await run_agent(core, org["ceo"]["id"], "manual") is None

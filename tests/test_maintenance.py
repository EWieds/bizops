from bizops.engine import create_task, human_message
from bizops.goals import set_goal
from bizops.maintenance import audit_checks, budget_alerts, daily_digest, goal_reviews, heartbeat_needed, run_jobs, stalled_tasks
from bizops.scheduler import Scheduler
from bizops.util import iso_in


def inbox(core, agent_id):
    return core.db.all("SELECT body, type FROM messages WHERE recipient_id = ? AND sender_kind = 'system' ORDER BY created_at",
                       (agent_id,))


def test_kickoff_message_on_org_creation(core, org):
    assert any(m["body"].startswith("Kickoff") for m in inbox(core, org["ceo"]["id"]))


def test_goal_review_wakes_owner_once(core, org):
    g = set_goal(core.db, core.settings, setter=None, owner_id=org["coo"]["id"], title="Ship", metric="pages", target=3)
    core.db.execute("UPDATE goals SET next_review_at = ? WHERE id = ?", (iso_in(-5), g["id"]))
    assert goal_reviews(core.db, core.settings) == 1
    assert goal_reviews(core.db, core.settings) == 0  # rescheduled
    assert "Goal review due" in inbox(core, org["coo"]["id"])[-1]["body"]


def test_stalled_task_nudge_then_escalate(core, org):
    t = create_task(core.db, creator=org["coo"], assignee_id=org["researcher"]["id"], title="Research", deliverable="doc")
    core.db.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (iso_in(-7 * 3600), t["id"]))
    assert stalled_tasks(core.db, core.settings) == 1
    assert "Stalled task" in inbox(core, org["researcher"]["id"])[-1]["body"]
    core.db.execute("UPDATE tasks SET last_nudged_at = ? WHERE id = ?", (iso_in(-7 * 3600), t["id"]))
    stalled_tasks(core.db, core.settings)
    assert any(m["type"] == "escalation" for m in inbox(core, org["coo"]["id"]))


def test_budget_alert_once_a_day(core, org):
    from bizops import ledger
    ledger.record(core.db, org_id=org["org"], kind="spend", amount_micros=45_000_000, source="system")
    assert budget_alerts(core.db, core.settings) == 1
    assert budget_alerts(core.db, core.settings) == 0
    assert any("Budget alert" in m["body"] for m in inbox(core, org["cfo"]["id"]))


def test_daily_digest_once(core, org):
    core.settings.policy["heartbeats"]["digest_hour_utc"] = 0
    assert daily_digest(core.db, core.settings) is True
    assert daily_digest(core.db, core.settings) is False
    assert any("Daily digest" in m["body"] for m in inbox(core, org["pceo"]["id"]))


def test_audit_checks_flag_unsupported_claims(core, org):
    g = set_goal(core.db, core.settings, setter=None, owner_id=org["ceo"]["id"], title="Rev", metric="usd", target=100)
    core.db.execute("UPDATE goals SET status = 'achieved' WHERE id = ?", (g["id"],))
    assert audit_checks(core.db, core.settings) == 1
    assert audit_checks(core.db, core.settings) == 0  # deduped
    f = core.db.one("SELECT * FROM audit_findings")
    assert f["kind"] == "unsupported_claim" and f["source"] == "system"


def test_heartbeat_skips_idle_worker(core, org):
    w = core.db.one("SELECT * FROM agents WHERE id = ?", (org["writer"]["id"],))
    assert heartbeat_needed(core.db, core.settings, w) == (False, "nothing to act on")
    human_message(core.db, core.settings, w["id"], "hi")
    assert heartbeat_needed(core.db, core.settings, w)[0]


async def test_scheduler_skips_idle_heartbeat_for_free(core, org):
    core.db.execute("UPDATE agents SET wake_requested_at = NULL, next_wake_at = NULL")
    core.db.execute("UPDATE messages SET status = 'read'")
    core.db.execute("UPDATE agents SET next_wake_at = ?, wake_interval_s = 3600 WHERE id = ?", (iso_in(-1), org["writer"]["id"]))
    s = Scheduler(core)
    s._last_sweep = 1e18  # no maintenance during this tick
    assert await s.tick() == 0
    assert core.db.scalar("SELECT COUNT(*) AS n FROM runs") == 0
    assert core.db.scalar("SELECT COUNT(*) AS n FROM events WHERE action = 'heartbeat.skip'") == 1


def test_run_jobs_force(core, org):
    out = run_jobs(core.db, core.settings, force=True)
    assert set(out) >= {"expired", "goal_reviews", "stalled", "budget_alerts", "digest", "findings"}

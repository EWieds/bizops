from bizops.engine import create_task
from bizops.gateway import invoke
from bizops.maintenance import management_reviews
from bizops.tools import Ctx


async def test_team_report_shows_real_work(core, org):
    w = Ctx(core=core, agent=org["writer"])
    assert (await invoke(w, "write_artifact", {"name": "landing-copy.md", "content": "Buy now!!!"})).ok
    r = await invoke(Ctx(core=core, agent=org["ceo"]), "team_report", {"hours": 24})
    assert r.ok and "landing-copy.md" in r.content and "Copywriter" in r.content
    r = await invoke(Ctx(core=core, agent=org["writer"]), "team_report", {"agent_id": org["ceo"]["id"]})
    assert not r.ok and "team.scope" in r.content


async def test_request_review_creates_task_for_specialist(core, org):
    assert (await invoke(Ctx(core=core, agent=org["writer"]), "write_artifact", {"name": "copy.md", "content": "x"})).ok
    r = await invoke(Ctx(core=core, agent=org["ceo"]), "request_review",
                     {"reviewer": org["marketer"]["id"], "artifact_id": "copy.md", "feedback": "Too vague, add proof."})
    assert r.ok, r.content
    t = core.db.one("SELECT * FROM tasks WHERE assignee_id = ?", (org["marketer"]["id"],))
    assert t["title"].startswith("Review and improve") and "Too vague" in t["description"]
    r = await invoke(Ctx(core=core, agent=org["writer"]), "request_review",
                     {"reviewer": org["ceo"]["id"], "artifact_id": "copy.md", "feedback": "x"})
    assert not r.ok and "task.scope" in r.content


async def test_management_review_only_when_team_was_active(core, org):
    core.db.execute("DELETE FROM messages")
    assert management_reviews(core.db, core.settings) == 0  # nothing happened yet
    core.db.execute("DELETE FROM settings WHERE key LIKE 'mgmt_review:%'")
    await invoke(Ctx(core=core, agent=org["coo"]), "write_artifact", {"name": "plan.md", "content": "plan"})
    assert management_reviews(core.db, core.settings) >= 1
    msg = core.db.one("SELECT body FROM messages WHERE recipient_id = ? AND body LIKE 'Management review%'", (org["ceo"]["id"],))
    assert msg and "request_review" in msg["body"]
    assert management_reviews(core.db, core.settings) == 0  # not again until the next window


async def test_dry_run_ceo_sends_weak_work_for_review(core, org):
    from bizops.scheduler import Scheduler
    await invoke(Ctx(core=core, agent=org["coo"]), "write_artifact", {"name": "plan.md", "content": "plan"})
    core.db.execute("DELETE FROM settings WHERE key LIKE 'mgmt_review:%'")
    management_reviews(core.db, core.settings)
    core.db.execute("UPDATE agents SET min_interval_s = 0")
    await Scheduler(core, tick_s=0.05).drain(timeout=60)
    assert core.db.one("SELECT 1 AS x FROM events WHERE action = 'request_review' AND outcome = 'ok'")

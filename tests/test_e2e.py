from bizops import ledger
from bizops.engine import human_message, org_ceo
from bizops.scheduler import Scheduler
from bizops.seed import seed_demo


async def test_seeded_company_runs_end_to_end(core):
    ids = seed_demo(core)
    core.db.execute("UPDATE agents SET min_interval_s = 0")
    await Scheduler(core).drain(timeout=60)
    db = core.db
    org = ids["demo"]
    # delegation went down the tree and work came back
    assert db.scalar("SELECT COUNT(*) AS n FROM tasks WHERE org_id = ? AND status = 'done'", (org,)) >= 3
    assert db.scalar("SELECT COUNT(*) AS n FROM artifacts WHERE org_id = ?", (org,)) >= 1
    # the domain purchase went through the approver and the sandbox provider
    assert ledger.wallet_balance(db, org) == 31_000_000
    # the contract is in the human queue, task blocked, not waiting
    assert db.scalar("SELECT COUNT(*) AS n FROM policy_decisions WHERE verdict = 'hard_stop' AND status = 'open'") == 1
    assert db.scalar("SELECT COUNT(*) AS n FROM tasks WHERE status = 'blocked'") == 1
    # the owner got an answer
    assert db.scalar("SELECT COUNT(*) AS n FROM messages WHERE recipient_kind = 'human'") >= 1
    # every tool 'before' has an 'after'
    orphans = db.scalar("SELECT COUNT(*) AS n FROM events b WHERE b.phase = 'before' AND NOT EXISTS "
                        "(SELECT 1 FROM events a WHERE a.call_id = b.call_id AND a.phase = 'after')")
    assert orphans == 0
    # nothing left running or pending
    assert db.scalar("SELECT COUNT(*) AS n FROM runs WHERE status = 'running'") == 0


async def test_platform_ceo_answers_owner(core):
    ids = seed_demo(core)
    core.db.execute("UPDATE agents SET min_interval_s = 0")
    pceo = org_ceo(core.db, ids["platform"])
    human_message(core.db, core.settings, pceo["id"], "How are the businesses doing?")
    await Scheduler(core).drain(timeout=60)
    reply = core.db.one("SELECT * FROM messages WHERE sender_id = ? AND recipient_kind = 'human'", (pceo["id"],))
    assert reply and "orgs" in reply["body"]

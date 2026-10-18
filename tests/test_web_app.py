import pytest
from fastapi.testclient import TestClient

from bizops import ledger
from bizops.scheduler import Scheduler
from bizops.seed import seed_demo
from bizops.web.app import create_app


@pytest.fixture
async def seeded(core):
    ids = seed_demo(core)
    core.db.execute("UPDATE agents SET min_interval_s = 0")
    await Scheduler(core).drain(timeout=60)
    return ids


@pytest.fixture
def client(core, seeded):
    with TestClient(create_app(core, run_scheduler=False)) as c:
        yield c


def test_pages_render(core, seeded, client):
    agent = core.db.one("SELECT id FROM agents WHERE role = 'researcher'")
    ev = core.db.one("SELECT id FROM events ORDER BY id DESC")
    art = core.db.one("SELECT id FROM artifacts")
    for url in ["/", f"/orgs/{seeded['demo']}", f"/agents/{agent['id']}", "/activity", "/activity?outcome=refused&q=pay",
                "/activity/rows", f"/events/{ev['id']}", "/chat", "/queue", "/money", "/new", f"/artifacts/{art['id']}",
                "/healthz", "/goals", "/audit", "/system", "/live", f"/agents/{agent['id']}/prompt"]:
        assert client.get(url).status_code == 200, url


def test_chat_post_creates_message_and_wake(core, seeded, client):
    ceo = core.db.one("SELECT id FROM agents WHERE depth = 0 AND org_id = ?", (seeded["demo"],))
    r = client.post("/chat", data={"agent": ceo["id"], "text": "status?"}, follow_redirects=False)
    assert r.status_code == 303
    assert core.db.scalar("SELECT wake_requested_at FROM agents WHERE id = ?", (ceo["id"],))
    assert "status?" in client.get(f"/chat/thread?agent={ceo['id']}").text


def test_kill_switch_buttons(core, seeded, client):
    client.post("/pause", data={"target": "platform", "paused": "1"})
    assert "Platform paused" in client.get("/").text
    client.post("/pause", data={"target": "platform", "paused": "0"})
    client.post("/pause", data={"target": "org", "target_id": seeded["demo"], "paused": "1"})
    assert core.db.scalar("SELECT status FROM orgs WHERE id = ?", (seeded["demo"],)) == "paused"


def test_resolve_hard_stop_unblocks_task(core, seeded, client):
    d = core.db.one("SELECT * FROM policy_decisions WHERE verdict = 'hard_stop'")
    client.post(f"/decisions/{d['id']}/resolve", data={"note": "signed it", "outcome": "done"})
    assert core.db.scalar("SELECT status FROM policy_decisions WHERE id = ?", (d["id"],)) == "resolved"
    assert core.db.scalar("SELECT status FROM tasks WHERE id = ?", (d["task_id"],)) == "open"


def test_override_blocked_action(core, seeded, client):
    import asyncio

    from bizops.gateway import invoke
    from bizops.tools import Ctx
    core.db.execute("UPDATE agents SET budget_micros = 30000000 WHERE depth = 0 AND org_id = ?", (seeded["demo"],))
    ceo = core.db.one("SELECT * FROM agents WHERE depth = 0 AND org_id = ?", (seeded["demo"],))
    payee = core.db.one("SELECT id FROM payees")
    task = core.db.one("SELECT id FROM tasks WHERE assignee_id = ?", (ceo["id"],))
    r = asyncio.run(invoke(Ctx(core=core, agent=ceo), "pay", {"payee_id": payee["id"], "amount_usd": 3,
                                                               "memo": "BLOCKME", "task_id": task["id"]}))
    assert not r.ok
    d = core.db.one("SELECT * FROM policy_decisions WHERE verdict = 'blocked' AND action = 'pay'")
    before = ledger.wallet_balance(core.db, seeded["demo"])
    client.post(f"/decisions/{d['id']}/override")
    assert ledger.wallet_balance(core.db, seeded["demo"]) == before - 3_000_000
    assert core.db.scalar("SELECT status FROM policy_decisions WHERE id = ?", (d["id"],)) == "resolved"


def test_money_forms(core, seeded, client):
    before = ledger.wallet_balance(core.db, seeded["demo"])
    client.post("/money/deposit", data={"org_id": seeded["demo"], "amount": "10"})
    assert ledger.wallet_balance(core.db, seeded["demo"]) == before + 10_000_000
    client.post("/money/payee", data={"name": "Google Ads", "org_id": seeded["demo"], "max_usd": "30"})
    assert core.db.scalar("SELECT COUNT(*) AS n FROM payees") == 2


def test_new_business(core, seeded, client):
    r = client.post("/new", data={"name": "Gutter Leads", "template": "generic", "brief": "gutters in Ohio", "cap": "50"},
                    follow_redirects=False)
    assert r.status_code == 303
    org = core.db.one("SELECT * FROM orgs WHERE name = 'Gutter Leads'")
    assert org and org["brief"] == "gutters in Ohio"
    r = client.post("/new", data={"name": "Too Big", "template": "generic", "cap": "5000"})
    assert "exceed" in r.text or "ceiling" in r.text


def test_explain_routes_to_platform_ceo(core, seeded, client):
    ev = core.db.one("SELECT id FROM events WHERE action = 'pay' AND phase = 'after'")
    r = client.post("/explain", data={"event_id": ev["id"]}, follow_redirects=False)
    assert r.status_code == 303 and "/chat?agent=" in r.headers["location"]
    assert core.db.scalar("SELECT COUNT(*) AS n FROM messages WHERE body LIKE ?", (f"%{ev['id']}%",)) == 1


def test_goal_measure_and_finding_resolve(core, seeded, client):
    g = core.db.one("SELECT id FROM goals")
    client.post(f"/goals/{g['id']}/measure", data={"value": "7"})
    assert core.db.scalar("SELECT measured_value FROM goals WHERE id = ?", (g["id"],)) == 7
    from bizops.maintenance import _finding
    _finding(core.db, kind="other", severity="low", summary="x", evidence="y", dedupe="t1")
    fid = core.db.scalar("SELECT id FROM audit_findings")
    client.post(f"/audit/{fid}/resolve")
    assert core.db.scalar("SELECT status FROM audit_findings WHERE id = ?", (fid,)) == "resolved"

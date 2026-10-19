"""Regression tests for the bottlenecks seen in production: each one was a refusal or owner-queue item
that agents hit over and over. They must not come back."""

import json

import pytest

from bizops import maintenance, owner_setup
from bizops.business import record_inbound
from bizops.engine import create_org_from_template, create_task, org_ceo
from bizops.gateway import invoke
from bizops.ideas import validate_case
from bizops.tools import Ctx


def cio(core):
    return core.db.one("SELECT * FROM agents WHERE role = 'platform_cfo'")


def open_items(core, rule):
    return core.db.all("SELECT * FROM policy_decisions WHERE status = 'open' AND rule = ?", (rule,))


async def test_messages_by_role_name_reach_the_right_agent(core, org):
    r = await invoke(Ctx(core=core, agent=org["coo"]), "message", {"to": "ceo", "type": "status", "text": "done"})
    assert r.ok, r.content
    assert core.db.scalar("SELECT recipient_id FROM messages ORDER BY created_at DESC LIMIT 1") == org["ceo"]["id"]
    r = await invoke(Ctx(core=core, agent=org["ceo"]), "message", {"to": "platform_ceo", "type": "question", "text": "?"})
    assert r.ok, r.content
    r = await invoke(Ctx(core=core, agent=org["ceo"]), "message", {"to": "CIO", "type": "question", "text": "red team?"})
    assert r.ok, r.content
    r = await invoke(Ctx(core=core, agent=cio(core)), "message", {"to": org["ceo"]["id"], "type": "answer", "text": "PARK"})
    assert r.ok, r.content
    r = await invoke(Ctx(core=core, agent=org["coo"]), "message", {"to": "cio", "type": "status", "text": "x"})
    assert not r.ok and "message.scope" in r.content  # business staff go through their CEO
    r = await invoke(Ctx(core=core, agent=org["ceo"]), "create_task",
                     {"assignee": "coo", "title": "t", "deliverable": "a checkable thing"})
    assert r.ok, r.content


async def test_placeholder_task_id_gets_a_clear_refusal(core, org):
    r = await invoke(Ctx(core=core, agent=org["ceo"]), "update_task", {"task_id": "none", "status": "in_progress"})
    assert not r.ok and "list_tasks" in r.content


async def test_platform_setup_is_one_owner_item_and_never_blocks_tasks(core, org):
    root = org["root"]
    other = create_org_from_template(core.db, core.settings, template="classic", name="Other Co", parent_org_id=root,
                                     supervisor_agent_id=org["pceo"]["id"])
    other_ceo = org_ceo(core.db, other)
    task = create_task(core.db, creator=None, assignee_id=org["ceo"]["id"], title="launch", deliverable="live offer")
    asks = [(org["ceo"], {"category": "identity_account", "task_id": task["id"],
                          "summary": "Please connect the Stripe account so create_offer can make a payment link"}),
            (other_ceo, {"category": "external_transfer", "summary": "Add the Stripe key and connect SMTP + postal address"}),
            (org["pceo"], {"category": "identity_account", "summary": "create a $149/mo recurring payment link in Stripe"})]
    for agent, args in asks:
        r = await invoke(Ctx(core=core, agent=agent), "request_human", args)
        assert not r.ok and "NOT FILED" in r.content, r.content
    assert len(open_items(core, "hard_stop.connect_billing")) == 1
    assert len(open_items(core, "hard_stop.connect_email")) == 1
    assert not open_items(core, "hard_stop.identity_account") and not open_items(core, "hard_stop.external_transfer")
    assert core.db.scalar("SELECT status FROM tasks WHERE id = ?", (task["id"],)) != "blocked"

    core.connectors.billing.live = True
    assert await owner_setup.resolve_connected(core) == 1
    assert not open_items(core, "hard_stop.connect_billing")
    r = await invoke(Ctx(core=core, agent=org["ceo"]), "request_human",
                     {"category": "identity_account", "summary": "connect Stripe please"})
    assert "ALREADY connected" in r.content


async def test_other_owner_asks_still_file_but_only_once(core, org):
    args = {"category": "legal_commitment", "summary": "Sign the reseller agreement with Acme"}
    r = await invoke(Ctx(core=core, agent=org["ceo"]), "request_human", args)
    assert r.outcome == "hard_stop"
    r = await invoke(Ctx(core=core, agent=org["ceo"]), "request_human", args)
    assert not r.ok and "ALREADY IN THE OWNER'S QUEUE" in r.content
    assert len(open_items(core, "hard_stop.legal_commitment")) == 1


async def test_files_and_kb_are_readable_across_the_platform(core, org):
    biz, plat = Ctx(core=core, agent=org["ceo"]), Ctx(core=core, agent=cio(core))
    art = json.loads((await invoke(biz, "write_artifact", {"name": "offer.md", "content": "Offer v2: $49/mo"})).content)
    r = await invoke(plat, "read_artifact", {"artifact_id": art["artifact_id"]})
    assert r.ok and "$49/mo" in r.content
    assert (await invoke(biz, "kb_write", {"key": "gbp_offer_v2", "body": "price $49"})).ok
    r = await invoke(plat, "kb_read", {"key": "gbp_offer_v2"})
    assert r.ok and "price $49" in r.content
    memo = json.loads((await invoke(plat, "write_artifact", {"name": "redteam.md", "content": "PARK"})).content)
    r = await invoke(biz, "read_artifact", {"artifact_id": memo["artifact_id"]})
    assert r.ok and "PARK" in r.content
    other = create_org_from_template(core.db, core.settings, template="classic", name="Other Co", parent_org_id=org["root"],
                                     supervisor_agent_id=org["pceo"]["id"])
    r = await invoke(Ctx(core=core, agent=org_ceo(core.db, other)), "read_artifact", {"artifact_id": art["artifact_id"]})
    assert not r.ok and "files.missing" in r.content  # businesses don't read each other's files


async def test_quality_gate_sees_referenced_work(core, org):
    from bizops.decide import referenced_work
    biz = Ctx(core=core, agent=org["ceo"])
    art = json.loads((await invoke(biz, "write_artifact", {"name": "plan.md", "content": "Full plan text"})).content)
    await invoke(biz, "kb_write", {"key": "redteam_relay_lite", "body": "Verdict: PARK because..."})
    ev = referenced_work(core, f"Done: see {art['artifact_id']} and the memo in KB redteam_relay_lite.")
    assert any("Full plan text" in v for v in ev.values()) and any("PARK" in v for v in ev.values())


async def test_read_back_published_page_and_update_own_goal(core, org):
    from bizops.goals import set_goal
    pe = core.db.one("SELECT * FROM agents WHERE role IN ('coder','marketer') AND org_id = ? LIMIT 1", (org["org"],))
    core.db.execute("UPDATE agents SET grants_json = ? WHERE id = ?", (json.dumps(["comms", "site"]), pe["id"]))
    pe = core.db.one("SELECT * FROM agents WHERE id = ?", (pe["id"],))
    r = await invoke(Ctx(core=core, agent=pe), "publish_page", {"path": "index", "title": "Hi",
                                                               "html": "<h1>Hello</h1><p>Contact: a@b.co</p>"})
    assert r.ok, r.content
    r = await invoke(Ctx(core=core, agent=pe), "read_page", {"path": "index"})
    assert r.ok and "Contact: a@b.co" in r.content
    g = set_goal(core.db, core.settings, setter=org["ceo"], owner_id=pe["id"], title="ship", metric="pages",
                 target=1, due=7)
    r = await invoke(Ctx(core=core, agent=pe), "update_goal", {"goal_id": g["id"], "progress_note": "shipped"})
    assert r.ok, r.content


def test_business_case_sent_as_json_string_is_parsed():
    with pytest.raises(Exception) as e:
        validate_case(json.dumps({"pricing": {"price_usd": 10}}), starting_budget_usd=50)
    assert "needs a business_case" not in str(e.value)


async def test_recurring_refusals_become_audit_findings(core, org):
    for _ in range(5):
        await invoke(Ctx(core=core, agent=org["coo"]), "message", {"to": "nobody-here", "type": "status", "text": "x"})
    assert maintenance.bottleneck_checks(core.db, core.settings) == 1
    f = core.db.one("SELECT * FROM audit_findings WHERE dedupe_key LIKE 'bottleneck:message.recipient:%'")
    assert f and "5 times" in f["summary"]


def test_business_address_mail_from_people_reaches_the_ceo(core, org):
    slug = core.db.scalar("SELECT slug FROM orgs WHERE id = ?", (org["org"],))
    human = record_inbound(core.db, org_id=None, from_addr="jane@dentist.com", to_addr=f"hello+{slug}@x.com",
                           subject="Question about pricing", body="Hi, how much?", provider="smtp", provider_id="h1")
    robot = record_inbound(core.db, org_id=None, from_addr="no-reply@tiktok.com", to_addr=f"hello+{slug}@x.com",
                           subject="Your code is 4411", body="4411", provider="smtp", provider_id="r1")
    assert core.db.scalar("SELECT processed FROM emails WHERE id = ?", (human,)) == 0
    assert core.db.scalar("SELECT processed FROM emails WHERE id = ?", (robot,)) == 1


def test_agents_see_setup_status(core, org):
    from bizops.runtime import gather_context
    text, _, _ = gather_context(core, org["ceo"], "heartbeat")
    assert "Platform setup" in text and "Never file it" in text


async def test_tool_level_setup_stop_creates_one_clear_item(core, org, monkeypatch):
    from bizops.tools import sales
    monkeypatch.setattr(sales, "sandbox_world", lambda ctx: False)
    for _ in range(2):
        r = await invoke(Ctx(core=core, agent=org["ceo"]), "create_offer",
                         {"name": "Plan", "description": "Monthly plan for dentists", "price_usd": 49, "interval": "month"})
        assert r.outcome == "hard_stop", r.content
    items = open_items(core, "hard_stop.connect_billing")
    assert len(items) == 1 and "One-time setup for every business" in items[0]["args_json"]

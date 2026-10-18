import json

import httpx
import pytest

from bizops import business, ledger
from bizops.connectors.billing import StripeBilling
from bizops.connectors.email import SMTPEmail
from bizops.connectors.voice import VapiVoice
from bizops.engine import org_ceo
from bizops.gateway import invoke
from bizops.scheduler import Scheduler
from bizops.seed import seed_venture
from bizops.tools import Ctx


async def run_until_quiet(core, rounds=4):
    s = Scheduler(core, tick_s=0.05)
    for _ in range(rounds):
        core.db.execute("UPDATE agents SET min_interval_s = 0")
        await s.drain(timeout=60)


async def test_venture_loop_end_to_end(core):
    """Owner asks for a business -> platform researches, launches it -> build, sell, get paid, deliver."""
    seed_venture(core)
    await run_until_quiet(core)
    db = core.db
    org = db.one("SELECT * FROM orgs WHERE template = 'voice-agent-sales'")
    assert org and org["status"] == "trial"
    assert db.scalar("SELECT COUNT(*) AS n FROM sites WHERE org_id = ?", (org["id"],)) >= 1
    assert db.scalar("SELECT COUNT(*) AS n FROM offers WHERE org_id = ?", (org["id"],)) == 1
    assert db.scalar("SELECT COUNT(*) AS n FROM leads WHERE org_id = ?", (org["id"],)) >= 4
    out = db.all("SELECT * FROM emails WHERE direction = 'out' AND org_id = ?", (org["id"],))
    assert out and all("/u/" in e["body"] and "Unsubscribe" in e["body"] for e in out)
    paid = db.all("SELECT * FROM payments_in WHERE org_id = ?", (org["id"],))
    assert paid and all(p["sandbox"] == 1 for p in paid)
    live = db.all("SELECT * FROM customers WHERE org_id = ? AND status = 'live'", (org["id"],))
    assert live and all(c["voice_agent_id"] for c in live)
    assert db.scalar("SELECT COUNT(*) AS n FROM voice_agents WHERE org_id = ? AND status = 'live'", (org["id"],)) == len(live)
    goal = db.one("SELECT * FROM goals WHERE org_id = ? AND metric_key = 'revenue_usd'", (org["id"],))
    assert goal["measured_value"] == sum(p["amount_cents"] for p in paid) / 100 and goal["measured_source"] == "sandbox"
    # test money never counts as real revenue
    assert ledger.revenue(db, since="2000") == 0
    assert ledger.revenue(db, since="2000", sandbox=True) == sum(p["amount_cents"] for p in paid) * 10_000
    assert db.scalar("SELECT COUNT(*) AS n FROM events WHERE outcome = 'error'") == 0


@pytest.fixture
def sales_org(core):
    from bizops.engine import create_org_from_template
    from bizops.seed import ensure_platform
    root = ensure_platform(core)
    oid = create_org_from_template(core.db, core.settings, template="generic", name="Sell Co", parent_org_id=root,
                                   supervisor_agent_id=org_ceo(core.db, root)["id"], kickoff=False)
    agents = {a["role"]: a for a in core.db.all("SELECT * FROM agents WHERE org_id = ?", (oid,))}
    lead = business.add_lead(core.db, org_id=oid, agent=agents["sales"], company="Acme Plumbing", email="owner@acmeplumbing.com",
                             source_url="https://acmeplumbing.com/contact")
    return {"org": oid, "lead": lead, **agents}


async def test_outreach_rules(core, sales_org):
    sales = sales_org["sales"]
    c = Ctx(core=core, agent=sales)
    lid = sales_org["lead"]["id"]
    r = await invoke(c, "send_email", {"lead_id": lid, "subject": "Missed calls", "body": "Hi there"})
    assert r.ok, r.content
    r = await invoke(c, "send_email", {"lead_id": lid, "subject": "Following up", "body": "Hi again"})
    assert not r.ok and "followup_gap" in r.content
    r = await invoke(c, "send_email", {"lead_id": lid, "subject": "Guaranteed results", "body": "100% free, act now"})
    assert not r.ok and "approver" in r.content
    business.suppress(core.db, "owner@acmeplumbing.com", "test")
    r = await invoke(c, "send_email", {"lead_id": lid, "subject": "x", "body": "y", "kind": "reply"})
    assert not r.ok and "suppressed" in r.content


async def test_b2b_source_required_and_dedupe(core, sales_org):
    c = Ctx(core=core, agent=sales_org["sales"])
    r = await invoke(c, "add_lead", {"company": "Nope LLC", "email": "a@nope.com", "source_url": ""})
    assert not r.ok and "lead.source" in r.content
    r = await invoke(c, "add_lead", {"company": "Acme Plumbing", "email": "owner@acmeplumbing.com", "source_url": "x"})
    assert not r.ok and "duplicate" in r.content


async def test_daily_cap(core, sales_org):
    core.settings.policy["sales"]["email_daily_max_per_org"] = 1
    c = Ctx(core=core, agent=sales_org["sales"])
    other = business.add_lead(core.db, org_id=sales_org["org"], agent=sales_org["sales"], company="B Co", email="b@bco.com",
                              source_url="https://bco.com")
    assert (await invoke(c, "send_email", {"lead_id": sales_org["lead"]["id"], "subject": "Hi", "body": "Hello"})).ok
    r = await invoke(c, "send_email", {"lead_id": other["id"], "subject": "Hi", "body": "Hello"})
    assert not r.ok and "daily_max" in r.content


def test_reply_unsubscribe_is_honored(core, sales_org):
    business.record_inbound(core.db, org_id=None, from_addr="owner@acmeplumbing.com", to_addr="us", subject="Re: hi",
                            body="Please remove me from your list", provider="smtp", provider_id="m1")
    business.process_inbound(core.db, core.settings)
    assert core.db.one("SELECT 1 AS x FROM suppressions WHERE email = 'owner@acmeplumbing.com'")
    assert core.db.scalar("SELECT stage FROM leads WHERE id = ?", (sales_org["lead"]["id"],)) == "unsubscribed"


def test_unsubscribe_token_roundtrip(settings):
    tok = business.unsub_token(settings, "A@B.com")
    assert business.unsub_email(settings, tok) == "a@b.com"
    assert business.unsub_email(settings, tok[:-1] + "0") is None


async def test_real_world_requires_connected_accounts(core, sales_org):
    """Outside the dry-run world, real leads need a real email account and offers need Stripe: one queue item each."""
    core.settings.policy["sandbox"]["force"] = False
    from bizops.llm.claude_cli import ClaudeCLIClient
    core.llm = ClaudeCLIClient(core.settings, binary="/bin/false")
    c = Ctx(core=core, agent=sales_org["sales"])
    for _ in range(2):
        r = await invoke(c, "send_email", {"lead_id": sales_org["lead"]["id"], "subject": "Hi", "body": "Hello"})
        assert r.outcome == "hard_stop"
    assert core.db.scalar("SELECT COUNT(*) AS n FROM policy_decisions WHERE rule = 'hard_stop.connect_email' AND status = 'open'") == 1
    r = await invoke(Ctx(core=core, agent=sales_org["ceo"]), "create_offer",
                     {"name": "X", "description": "d", "price_usd": 99, "interval": "month", "reason": "test"})
    assert r.outcome == "hard_stop" or "connect_billing" in r.content


async def test_live_email_needs_sender_identity(core, sales_org, monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SENDER_ADDRESS", "PO Box 1, Columbus OH")
    core.reload_connectors()
    with pytest.raises(Exception) as e:
        await business.send_email(core, agent=sales_org["sales"], lead_id=sales_org["lead"]["id"], subject="Hi", body="Hello")
    assert "SENDER_NAME" in str(e.value)


async def test_smtp_message_shape(settings, monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def ehlo(self): pass
        def has_extn(self, n): return True
        def starttls(self): pass
        def login(self, u, p): sent.append(("login", u))
        def send_message(self, m): sent.append(m)
    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    monkeypatch.setenv("SMTP_HOST", "smtp.x.com")
    monkeypatch.setenv("SMTP_USER", "me@x.com")
    monkeypatch.setenv("SMTP_PASSWORD", "pw")
    mid = await SMTPEmail(settings).send(from_addr="Biz <me@x.com>", to_addr="a@b.com", subject="Hi", body="Body",
                                         headers={"List-Unsubscribe": "<https://x/u/t>"})
    msg = sent[-1]
    assert msg["To"] == "a@b.com" and msg["List-Unsubscribe"] == "<https://x/u/t>" and mid


async def test_stripe_offer_labels_and_payment_polling(core, sales_org, monkeypatch):
    calls = []

    def handler(req: httpx.Request):
        body = dict(httpx.QueryParams(req.content.decode())) if req.content else {}
        calls.append((req.method, req.url.path, body))
        if req.url.path == "/v1/products":
            return httpx.Response(200, json={"id": "prod_1"})
        if req.url.path == "/v1/prices":
            return httpx.Response(200, json={"id": "price_1"})
        if req.url.path == "/v1/payment_links":
            return httpx.Response(200, json={"id": "plink_1", "url": "https://buy.stripe.com/test_1"})
        if req.url.path == "/v1/events":
            return httpx.Response(200, json={"data": [{"id": "evt_2", "data": {"object": {
                "id": "cs_1", "payment_status": "paid", "amount_total": 14900, "currency": "usd", "payment_link": "plink_1",
                "client_reference_id": sales_org["lead"]["id"], "customer_details": {"email": "owner@acmeplumbing.com"}}}}]})
        return httpx.Response(404)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    core.reload_connectors()
    core.connectors.billing = StripeBilling(core.settings, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    offer = await business.create_offer(core, agent=sales_org["ceo"], name="AI Receptionist", description="d",
                                        price_usd=149, interval="month")
    prod = calls[0][2]
    assert prod["metadata[bizops_org]"] == sales_org["org"] and prod["metadata[bizops_business]"] == "Sell Co"
    link = calls[2][2]
    assert link["subscription_data[metadata][bizops_org]"] == sales_org["org"]
    assert offer["payment_url"] == "https://buy.stripe.com/test_1"
    assert "client_reference_id=" in core.connectors.billing.link_for(offer, "lead_1", "")
    assert await business.poll_billing(core) == 1
    assert await business.poll_billing(core) == 0  # idempotent
    assert ledger.revenue(core.db, since="2000", org_id=sales_org["org"]) == 149_000_000  # real, labeled to this business
    cust = core.db.one("SELECT * FROM customers WHERE org_id = ?", (sales_org["org"],))
    assert cust["company"] == "Acme Plumbing" and cust["mrr_cents"] == 14900 and cust["sandbox"] == 0
    assert core.db.scalar("SELECT stage FROM leads WHERE id = ?", (sales_org["lead"]["id"],)) == "won"


async def test_vapi_upsert(settings, monkeypatch):
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path, json.loads(req.content)))
        return httpx.Response(200, json={"id": "asst_1"})
    monkeypatch.setenv("VAPI_API_KEY", "k")
    v = VapiVoice(settings, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    r = await v.upsert(agent_id="va_1", provider_ref=None, cfg={"business_name": "Joe's HVAC", "hours": "9-5"})
    assert r["provider_ref"] == "asst_1" and seen[0][1] == "/assistant"
    assert "Joe's HVAC" in seen[0][2]["model"]["messages"][0]["content"]
    await v.upsert(agent_id="va_1", provider_ref="asst_1", cfg={"business_name": "Joe's HVAC"})
    assert seen[1][0] == "PATCH" and seen[1][1] == "/assistant/asst_1"


async def test_create_business_venture_limits(core, org):
    c = Ctx(core=core, agent=org["pceo"])
    brief = "x" * 250
    r = await invoke(c, "create_business", {"name": "V1", "template": "generic", "brief": "too short"})
    assert not r.ok and "brief" in r.content
    r = await invoke(c, "create_business", {"name": "V1", "template": "nope", "brief": brief})
    assert not r.ok
    core.settings.policy["platform"]["monthly_cap_usd"] = 1000
    assert (await invoke(c, "create_business", {"name": "V1", "template": "generic", "brief": brief})).ok
    assert (await invoke(c, "create_business", {"name": "V2", "template": "generic", "brief": brief})).ok
    r = await invoke(c, "create_business", {"name": "V3", "template": "generic", "brief": brief})
    assert not r.ok and "venture" in r.content
    r = await invoke(Ctx(core=core, agent=org["ceo"]), "create_business", {"name": "V4", "template": "generic", "brief": brief})
    assert not r.ok and "grant" in r.content

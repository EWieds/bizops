import pytest
from fastapi.testclient import TestClient

from bizops import ledger, purchases
from bizops.business import record_inbound
from bizops.cards import SandboxCards
from bizops.gateway import invoke
from bizops.payments import deposit
from bizops.tools import Ctx
from bizops.util import iso, now

from conftest import with_csrf


class RecordingCards(SandboxCards):
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.created: list[dict] = []
        self.states: dict[str, str] = {}

    async def create(self, *, memo, limit_cents, period):
        if self.fail:
            raise RuntimeError("issuer down")
        card = await super().create(memo=memo, limit_cents=limit_cents, period=period)
        self.created.append({"limit_cents": limit_cents, "period": period, **card})
        self.states[card["token"]] = "OPEN"
        return card

    async def set_state(self, token, state):
        self.states[token] = state


@pytest.fixture
def cards(core):
    core.cards = RecordingCards()
    return core.cards


@pytest.fixture
def cio(core, org):
    return core.db.one("SELECT * FROM agents WHERE role = 'platform_cfo'")


def ask(core, org, amount=99, period="monthly", **extra):
    args = {"vendor": "Higgsfield", "item": "Creator plan", "amount_usd": amount, "period": period,
            "why": "AI video for the TikTok channel", "reason": "growth goal", **extra}
    return invoke(Ctx(core=core, agent=org["ceo"]), "request_purchase", args)


def decide(core, cio, pid, decision="approve", why="clear return, fair price"):
    return invoke(Ctx(core=core, agent=cio), "decide_purchase", {"purchase_id": pid, "decision": decision, "why": why})


def pid_of(core):
    return core.db.scalar("SELECT id FROM purchases ORDER BY created_at DESC LIMIT 1")


async def test_request_notifies_cio_and_approval_issues_card(core, org, cio, cards):
    deposit(core.db, org_id=org["org"], amount_usd=150)
    r = await ask(core, org)
    assert r.ok, r.content
    pid = pid_of(core)
    note = core.db.one("SELECT body FROM messages WHERE recipient_id = ? ORDER BY created_at DESC", (cio["id"],))
    assert pid in note["body"] and "decide_purchase" in note["body"]
    assert ledger.wallet_balance(core.db, org["org"]) == 150_000_000  # nothing moves until approved

    r = await decide(core, cio, pid)
    assert r.ok, r.content
    p = purchases.get(core.db, pid)
    assert p["status"] == "approved" and p["card_last4"] == "1111" and p["next_charge_at"]
    assert cards.created == [{"limit_cents": 9900, "period": "monthly", "token": p["card_token"], "last4": "1111"}]
    assert ledger.wallet_balance(core.db, org["org"]) == 51_000_000
    assert ledger.spent(core.db, since="2000", org_id=org["org"]) == 0  # never eats the model budget
    msg = core.db.one("SELECT body FROM messages WHERE recipient_id = ? ORDER BY created_at DESC", (org["ceo"]["id"],))
    assert "APPROVED" in msg["body"] and f"{{{{card:{pid}:number}}}}" in msg["body"]


async def test_wallet_and_limits_are_hard(core, org, cio, cards):
    deposit(core.db, org_id=org["org"], amount_usd=120)
    r = await ask(core, org, amount=500)
    assert not r.ok and "purchase.max" in r.content
    assert (await ask(core, org, amount=99)).ok
    r = await ask(core, org, amount=30, period="once")  # 99 is already pending against the wallet
    assert not r.ok and "purchase.wallet" in r.content
    core.settings.policy["purchases"]["max_monthly_subscriptions_usd_per_org"] = 100
    assert (await decide(core, cio, pid_of(core))).ok
    deposit(core.db, org_id=org["org"], amount_usd=100)
    r = await ask(core, org, amount=20)
    assert not r.ok and "purchase.subscriptions_max" in r.content


async def test_only_the_cio_decides_and_never_their_own(core, org, cio, cards):
    deposit(core.db, org_id=org["org"], amount_usd=100)
    assert (await ask(core, org, amount=10, period="once")).ok
    pid = pid_of(core)
    r = await invoke(Ctx(core=core, agent=org["ceo"]), "decide_purchase",
                     {"purchase_id": pid, "decision": "approve", "why": "me"})
    assert not r.ok and "grant" in r.content
    with pytest.raises(Exception, match="self_approval"):
        await purchases.decide(core, purchase_id=pid, decider=org["ceo"]["id"], approve=True, reason="mine")
    assert (await decide(core, cio, pid, "deny", "no measured need yet")).ok
    assert purchases.get(core.db, pid)["status"] == "denied"
    assert ledger.wallet_balance(core.db, org["org"]) == 100_000_000
    r = await decide(core, cio, pid)
    assert not r.ok and "not pending" in r.content


async def test_card_failure_refunds_wallet(core, org, cio):
    core.cards = RecordingCards(fail=True)
    deposit(core.db, org_id=org["org"], amount_usd=50)
    assert (await ask(core, org, amount=40, period="once")).ok
    r = await decide(core, cio, pid_of(core))
    assert not r.ok
    assert purchases.get(core.db, pid_of(core))["status"] == "failed"
    assert ledger.wallet_balance(core.db, org["org"]) == 50_000_000


async def test_monthly_renewal_pauses_and_resumes(core, org, cio, cards):
    deposit(core.db, org_id=org["org"], amount_usd=150)
    assert (await ask(core, org, amount=99)).ok
    pid = pid_of(core)
    assert (await decide(core, cio, pid)).ok
    token = purchases.get(core.db, pid)["card_token"]
    core.db.execute("UPDATE purchases SET next_charge_at = ? WHERE id = ?", (iso(now()), pid))
    assert await purchases.renew_due(core) == 1  # $51 left can't cover $99
    assert purchases.get(core.db, pid)["status"] == "paused" and cards.states[token] == "PAUSED"
    assert ledger.wallet_balance(core.db, org["org"]) == 51_000_000
    deposit(core.db, org_id=org["org"], amount_usd=100)
    assert await purchases.renew_due(core) == 1
    p = purchases.get(core.db, pid)
    assert p["status"] == "approved" and cards.states[token] == "OPEN" and p["next_charge_at"] > iso()
    assert ledger.wallet_balance(core.db, org["org"]) == 52_000_000


async def test_card_values_only_for_own_approved_purchase(core, org, cio, cards):
    deposit(core.db, org_id=org["org"], amount_usd=100)
    assert (await ask(core, org, amount=10, period="once")).ok
    pid = pid_of(core)
    with pytest.raises(Exception, match="pending"):
        await purchases.card_values(core, org_id=org["org"], purchase_id=pid)
    assert (await decide(core, cio, pid)).ok
    with pytest.raises(Exception, match="purchase.unknown"):
        await purchases.card_values(core, org_id=org["root"], purchase_id=pid)
    v = await purchases.card_values(core, org_id=org["org"], purchase_id=pid)
    assert v["number"] == "4111111111111111" and v["exp"] == "12/30"
    r = await invoke(Ctx(core=core, agent=org["ceo"]), "cancel_purchase", {"purchase_id": pid})
    assert r.ok and cards.states[purchases.get(core.db, pid)["card_token"]] == "CLOSED"


async def test_signup_mail_is_routed_by_plus_address(core, org):
    slug = core.db.scalar("SELECT slug FROM orgs WHERE id = ?", (org["org"],))
    eid = record_inbound(core.db, org_id=None, from_addr="security@mail.instagram.com",
                         to_addr=f"Hello <hello+{slug}@example.com>", subject="Your code is 123456",
                         body="Use 123456 to confirm your account.", provider="smtp", provider_id="m1")
    assert eid
    e = core.db.one("SELECT * FROM emails WHERE id = ?", (eid,))
    assert e["org_id"] == org["org"] and e["status"] == "account_mail" and e["processed"] == 1
    assert record_inbound(core.db, org_id=None, from_addr="x@y.com", to_addr="hello@example.com",
                          subject="s", body="b", provider="smtp", provider_id="m2") is None
    core.db.execute("UPDATE agents SET grants_json = json_insert(grants_json, '$[#]', 'browser') WHERE id = ?",
                    (org["ceo"]["id"],))
    r = await invoke(Ctx(core=core, agent=org["ceo"]), "read_account_mail", {"from_contains": "instagram"})
    assert r.ok and "123456" in r.content


async def test_browser_assist_goes_to_owner_queue(core, org):
    r = await invoke(Ctx(core=core, agent=org["ceo"]), "request_human",
                     {"category": "browser_assist", "summary": "TikTok sent a code to the owner's phone"})
    assert r.outcome == "hard_stop"
    assert core.db.one("SELECT * FROM policy_decisions WHERE rule = 'hard_stop.browser_assist' AND status = 'open'")


def test_owner_can_decide_on_money_page(core, org, cio, cards):
    deposit(core.db, org_id=org["org"], amount_usd=50)
    purchases.request(core.db, core.settings, agent=org["ceo"], vendor="Canva", item="Pro", amount_usd=15,
                      period="monthly", reason="design assets")
    pid = pid_of(core)
    with TestClient(__import__("bizops.web.app", fromlist=["create_app"]).create_app(core, run_scheduler=False)) as c:
        with_csrf(c)
        assert "Canva" in c.get("/money").text
        assert c.get("/connect").status_code == 200
        r = c.post(f"/money/purchase/{pid}/approve", data={"note": "ok"}, follow_redirects=False)
        assert r.status_code == 303
        assert purchases.get(core.db, pid)["decided_by"] == "human"
        assert c.post(f"/money/purchase/{pid}/approve", data={}, follow_redirects=False).status_code == 409
        c.post(f"/money/purchase/{pid}/cancel", follow_redirects=False)
    assert purchases.get(core.db, pid)["status"] == "cancelled"


def test_existing_agents_get_new_grants_once(core, org):
    from bizops.core import upgrade_grants
    from bizops.util import dumps
    core.db.execute("UPDATE agents SET grants_json = ? WHERE role = 'platform_cfo'", (dumps(["comms"]),))
    core.db.execute("DELETE FROM settings WHERE key LIKE 'grants_upgrade:%'")
    assert upgrade_grants(core.db) >= 1
    assert "approve_purchases" in core.db.scalar("SELECT grants_json FROM agents WHERE role = 'platform_cfo'")
    assert upgrade_grants(core.db) == 0


def test_purchases_count_as_costs_in_verified_profit(core, org):
    from bizops.capital import compute_period
    from bizops.db import insert
    with core.db.tx() as conn:
        insert(conn, "ledger_entries", {"id": "le_buy", "ts": "2026-07-15T00:00:00Z", "org_id": org["org"],
                                        "agent_id": None, "run_id": None, "kind": "purchase", "amount_micros": 30_000_000,
                                        "source": "provider", "memo": "tool", "ref_id": "pur_x:1", "event_id": None})
    p = compute_period(core.db, core.settings, org["org"], "2026-07")
    assert p["costs_micros"] == 30_000_000 and p["verified_profit_micros"] == -30_000_000

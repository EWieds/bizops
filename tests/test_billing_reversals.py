"""Stripe money events beyond the first checkout: renewals, refunds, chargebacks. Profit must never be overstated."""

import httpx
import pytest

from bizops import business, ledger
from bizops.connectors.billing import StripeBilling, parse_event
from bizops.engine import org_ceo


@pytest.fixture
def sales_org(core):
    from bizops.engine import create_org_from_template
    from bizops.seed import ensure_platform
    root = ensure_platform(core)
    oid = create_org_from_template(core.db, core.settings, template="generic", name="Sell Co", parent_org_id=root,
                                   supervisor_agent_id=org_ceo(core.db, root)["id"], kickoff=False)
    agents = {a["role"]: a for a in core.db.all("SELECT * FROM agents WHERE org_id = ?", (oid,))}
    lead = business.add_lead(core.db, org_id=oid, agent=agents["sales"], company="Acme Plumbing",
                             email="owner@acmeplumbing.com", source_url="https://acmeplumbing.com/contact")
    return {"org": oid, "lead": lead, **agents}


class FakeStripe:
    """Serves /v1/events newest first with ending_before paging, like Stripe."""

    def __init__(self):
        self.events: list[dict] = []
        self.seen_types: list[str] = []

    def add(self, type_: str, obj: dict) -> None:
        self.events.append({"id": f"evt_{len(self.events) + 1:04d}", "type": type_, "data": {"object": obj}})

    def handler(self, req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/products":
            return httpx.Response(200, json={"id": "prod_1"})
        if req.url.path == "/v1/prices":
            return httpx.Response(200, json={"id": "price_1"})
        if req.url.path == "/v1/payment_links":
            return httpx.Response(200, json={"id": "plink_1", "url": "https://buy.stripe.com/test_1"})
        if req.url.path == "/v1/events":
            q = req.url.params
            self.seen_types = q.get_list("types[]")
            newer = [e for e in self.events if not q.get("ending_before") or e["id"] > q["ending_before"]]
            limit = int(q.get("limit", 10))
            page = newer[:limit]  # the oldest `limit` events newer than the cursor...
            return httpx.Response(200, json={"data": list(reversed(page)), "has_more": len(newer) > limit})  # ...newest first
        return httpx.Response(404)


async def _setup(core, sales_org, monkeypatch):
    fake = FakeStripe()
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    core.reload_connectors()
    core.connectors.billing = StripeBilling(core.settings, http=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    offer = await business.create_offer(core, agent=sales_org["ceo"], name="AI Receptionist", description="d",
                                        price_usd=100, interval="month")
    fake.add("checkout.session.completed", {
        "id": "cs_1", "payment_status": "paid", "amount_total": 10000, "currency": "usd", "payment_link": "plink_1",
        "client_reference_id": sales_org["lead"]["id"], "customer_details": {"email": "owner@acmeplumbing.com"},
        "subscription": "sub_1", "invoice": "in_1", "payment_intent": None})
    return fake, offer


def rev(core, org):
    return ledger.revenue(core.db, since="2000", org_id=org)


async def test_renewal_refund_and_chargeback(core, sales_org, monkeypatch):
    fake, offer = await _setup(core, sales_org, monkeypatch)
    org = sales_org["org"]
    assert await business.poll_billing(core) == 1
    assert "charge.refunded" in fake.seen_types and "invoice.paid" in fake.seen_types
    assert rev(core, org) == 100_000_000

    # The first invoice of a subscription is the checkout above: not counted twice.
    fake.add("invoice.paid", {"id": "in_1", "billing_reason": "subscription_create", "amount_paid": 10000,
                              "subscription": "sub_1"})
    # Month 2 renewal: no client_reference_id, found through the subscription.
    fake.add("invoice.paid", {"id": "in_2", "billing_reason": "subscription_cycle", "amount_paid": 10000, "currency": "usd",
                              "subscription": "sub_1", "payment_intent": "pi_2", "customer_email": "owner@acmeplumbing.com",
                              "lines": {"data": [{"price": {"id": "price_1"}}]}})
    assert await business.poll_billing(core) == 1
    assert rev(core, org) == 200_000_000
    custs = core.db.all("SELECT * FROM customers WHERE org_id = ?", (org,))
    assert len(custs) == 1  # the renewal belongs to the same customer

    # Partial refund of the renewal, then the rest (Stripe reports the cumulative amount refunded).
    fake.add("charge.refunded", {"id": "ch_2", "payment_intent": "pi_2", "amount_refunded": 4000})
    assert await business.poll_billing(core) == 1
    assert rev(core, org) == 160_000_000
    fake.add("charge.refunded", {"id": "ch_2", "payment_intent": "pi_2", "amount_refunded": 10000})
    assert await business.poll_billing(core) == 1
    assert rev(core, org) == 100_000_000
    p2 = core.db.one("SELECT * FROM payments_in WHERE provider_ref = 'in_2'")
    assert p2["status"] == "refunded" and p2["refunded_cents"] == 10000

    # A replayed refund event never reverses twice.
    business.record_refund(core.db, core.settings, p2, refunded_total_cents=10000)
    assert rev(core, org) == 100_000_000

    # Chargeback on the first payment (found by invoice): reversed at once, restored only if won.
    fake.add("charge.dispute.created", {"id": "dp_1", "charge": "ch_1", "amount": 10000, "status": "needs_response"})
    assert await business.poll_billing(core) == 0  # charge ch_1 unknown yet: no match, nothing applied
    p1 = core.db.one("SELECT * FROM payments_in WHERE provider_ref = 'cs_1'")
    change = business.record_dispute(core.db, core.settings, p1, event="created", status="needs_response", amount_cents=10000)
    assert change == -10000 and rev(core, org) == 0
    assert business.record_dispute(core.db, core.settings, p1, event="funds_withdrawn", status="needs_response",
                                   amount_cents=10000) == 0  # no double reversal
    assert core.db.one("SELECT 1 AS x FROM policy_decisions WHERE rule = 'hard_stop.billing_dispute' AND status = 'open'")
    assert business.record_dispute(core.db, core.settings, p1, event="closed", status="won", amount_cents=10000) == 10000
    assert rev(core, org) == 100_000_000
    assert core.db.scalar("SELECT status FROM payments_in WHERE id = ?", (p1["id"],)) == "paid"


async def test_lost_dispute_and_cleared_revenue(core, sales_org, monkeypatch):
    fake, offer = await _setup(core, sales_org, monkeypatch)
    org = sales_org["org"]
    await business.poll_billing(core)
    p = core.db.one("SELECT * FROM payments_in WHERE provider_ref = 'cs_1'")
    core.db.execute("UPDATE payments_in SET provider_charge = 'ch_9', clears_at = '2000-01-01' WHERE id = ?", (p["id"],))
    assert business.cleared_revenue_cents(core.db, org_id=org) == 10000
    fake.add("charge.dispute.created", {"id": "dp_9", "charge": "ch_9", "amount": 10000, "status": "needs_response"})
    fake.add("charge.dispute.closed", {"id": "dp_9", "charge": "ch_9", "amount": 10000, "status": "lost"})
    assert await business.poll_billing(core) == 1  # opened (reversed); the loss itself moves no more money
    assert rev(core, org) == 0
    assert business.cleared_revenue_cents(core.db, org_id=org) == 0  # cleared profit can't count disputed money
    assert core.db.scalar("SELECT status FROM payments_in WHERE id = ?", (p["id"],)) == "dispute_lost"
    kinds = [r["kind"] for r in core.db.all("SELECT kind FROM ledger_entries WHERE org_id = ? AND kind LIKE 'revenue%' "
                                             "ORDER BY ts", (org,))]
    assert kinds == ["revenue", "revenue_reversal"]  # append-only: reversals are new rows


def test_parse_ignores_unpaid_and_foreign_events():
    assert parse_event({"id": "e", "type": "checkout.session.completed",
                        "data": {"object": {"id": "cs", "payment_status": "unpaid"}}}) is None
    assert parse_event({"id": "e", "type": "customer.created", "data": {"object": {"id": "cus"}}}) is None
    r = parse_event({"id": "e", "type": "charge.refunded", "data": {"object": {"id": "ch", "amount_refunded": 5}}})
    assert r["kind"] == "refund" and r["refunded_total_cents"] == 5


async def test_unknown_charge_is_ignored(core, sales_org, monkeypatch):
    fake, _ = await _setup(core, sales_org, monkeypatch)
    await business.poll_billing(core)
    fake.add("charge.refunded", {"id": "ch_other", "payment_intent": "pi_other", "amount_refunded": 999})
    assert await business.poll_billing(core) == 0
    assert rev(core, sales_org["org"]) == 100_000_000

"""Capital-allocation waterfall: verified profit only, reserves first, two keys, guardrails in code."""

from datetime import datetime, timezone

import pytest

from bizops import business, capital
from bizops.config import Settings
from bizops.core import Core
from bizops.engine import PolicyRefusal, create_org_from_template, org_ceo
from bizops.seed import ensure_platform
from bizops.util import new_id

AT = datetime(2026, 9, 15, tzinfo=timezone.utc)  # July 2026 is closed (ended + 30 days), August isn't yet
M = 1_000_000


@pytest.fixture
def core(tmp_path):
    s = Settings.load(home=tmp_path, db_path=tmp_path / "t.db", dry_run=True,
                      policy={"platform": {"monthly_cap_usd": 5000, "daily_cap_usd": 500, "hard_ceiling_usd": 10000},
                              "venture": {"research_every_days": 0, "starting_cap_usd": 50}})
    return Core(s).init()


def make_org(core, name, cap_usd=1000, status="active"):
    root = ensure_platform(core)
    oid = create_org_from_template(core.db, core.settings, template="generic", name=name, parent_org_id=root,
                                   supervisor_agent_id=org_ceo(core.db, root)["id"], kickoff=False)
    core.db.execute("UPDATE orgs SET monthly_cap_micros = ?, created_at = '2026-01-01T00:00:00Z', status = ? WHERE id = ?",
                    (cap_usd * M, status, oid))
    return oid


def agents(core, oid):
    return {a["role"]: a for a in core.db.all("SELECT * FROM agents WHERE org_id = ?", (oid,))}


def platform(core):
    return agents(core, core.db.scalar("SELECT id FROM orgs WHERE parent_org_id IS NULL"))


def book_month(core, oid, *, revenue_usd, refund_usd=0, cost_usd, month="2026-07"):
    """A month of history: a real payment (later partly refunded) and spend, dated inside `month`."""
    ceo = agents(core, oid)["ceo"]
    lead = business.add_lead(core.db, org_id=oid, agent=agents(core, oid)["sales"], company=f"C {new_id('x')}",
                             email=f"{new_id('p')}@corp.com", source_url="https://corp.com")
    offer = core.db.one("SELECT * FROM offers WHERE org_id = ?", (oid,))
    if not offer:
        offer_id = new_id("off")
        core.db.execute("INSERT INTO offers (id, org_id, name, description, price_cents, currency, interval, provider, "
                        "payment_url, status, created_by, created_at) VALUES (?, ?, 'Plan', 'd', 100, 'usd', "
                        "'one_time', 'stripe', 'https://x', 'active', ?, '2026-01-01')", (offer_id, oid, ceo["id"]))
    else:
        offer_id = offer["id"]
    r = business.record_payment(core.db, core.settings, provider="stripe", provider_ref=new_id("cs"),
                                amount_cents=int(revenue_usd * 100), offer_id=offer_id, lead_id=lead["id"],
                                payer_email=None, sandbox=False)
    core.db.execute("UPDATE payments_in SET paid_at = ?, clears_at = ? WHERE id = ?",
                    (f"{month}-10T00:00:00Z", f"{month}-28T00:00:00Z", r["payment_id"]))
    if refund_usd:
        p = core.db.one("SELECT * FROM payments_in WHERE id = ?", (r["payment_id"],))
        business.record_refund(core.db, core.settings, p, refunded_total_cents=int(refund_usd * 100))
    core.db.execute("INSERT INTO ledger_entries (id, ts, org_id, kind, amount_micros, source, memo) "
                    "VALUES (?, ?, ?, 'spend', ?, 'system', 'tools')", (new_id("le"), f"{month}-15T00:00:00Z", oid, int(cost_usd * M)))


def test_waterfall_numbers_and_idempotence(core):
    oid = make_org(core, "Winner")
    book_month(core, oid, revenue_usd=1000, refund_usd=100, cost_usd=100)
    assert capital.capital_cycle(core.db, core.settings, at=AT) == 3  # May, June (nothing), July
    p = core.db.one("SELECT * FROM allocation_periods WHERE org_id = ? AND month = '2026-07'", (oid,))
    assert p["cleared_revenue_micros"] == 900 * M          # net of the refund
    assert p["costs_micros"] == 100 * M
    assert p["verified_profit_micros"] == 800 * M
    assert p["tax_reserve_micros"] == 200 * M              # 25%
    assert p["operating_reserve_micros"] == 200 * M        # 2 months x $100 of costs
    assert p["surplus_micros"] == 400 * M
    assert capital.capital_cycle(core.db, core.settings, at=AT) == 0
    ceo = agents(core, oid)["ceo"]
    assert core.db.one("SELECT 1 AS x FROM messages WHERE recipient_id = ? AND body LIKE '2026-07 is closed%'", (ceo["id"],))


def test_unverified_revenue_is_never_allocatable(core):
    oid = make_org(core, "Pending")
    book_month(core, oid, revenue_usd=1000, cost_usd=100, month="2026-08")  # August isn't past its holdback yet
    capital.capital_cycle(core.db, core.settings, at=AT)
    assert not core.db.one("SELECT 1 AS x FROM allocation_periods WHERE month = '2026-08'")


def test_two_keys_and_org_guardrails(core):
    oid = make_org(core, "Winner")
    book_month(core, oid, revenue_usd=1000, refund_usd=100, cost_usd=100)
    capital.capital_cycle(core.db, core.settings, at=AT)
    p = core.db.one("SELECT * FROM allocation_periods WHERE org_id = ? AND month = '2026-07'", (oid,))
    team = agents(core, oid)
    ceo, cfo = team["ceo"], team["cfo"]
    kw = dict(scope="org", period_id=p["id"], reason="ads returned 3x last quarter", expected_result="+$300 MRR by Oct")

    with pytest.raises(PolicyRefusal, match="CEO"):  # first key is the CEO's
        capital.propose(core.db, core.settings, agent=cfo, amount_micros=100 * M, to_org_id=oid, **kw)
    with pytest.raises(PolicyRefusal) as e:  # cap may rise at most 25% ($250 of $1000) a month
        capital.propose(core.db, core.settings, agent=ceo, amount_micros=300 * M, to_org_id=oid, **kw)
    assert e.value.rule == "capital.cap_increase"
    with pytest.raises(PolicyRefusal) as e:  # at least 50% reinvested in self without a written reason
        capital.propose(core.db, core.settings, agent=ceo, amount_micros=300 * M, to_org_id=None, **kw)
    assert e.value.rule == "capital.min_reinvest_self"

    a = capital.propose(core.db, core.settings, agent=ceo, amount_micros=200 * M, to_org_id=oid, **kw)
    assert core.db.one("SELECT 1 AS x FROM messages WHERE recipient_id = ? AND body LIKE 'Second key needed%'", (cfo["id"],))
    with pytest.raises(PolicyRefusal, match="CFO"):  # the proposer can't be its own second key
        capital.review(core.db, core.settings, agent=ceo, allocation_id=a["id"], approve=True, score=8, reason="x")
    capital.review(core.db, core.settings, agent=cfo, allocation_id=a["id"], approve=True, score=8,
                   reason="payback < 2 months on past spend")
    assert core.db.scalar("SELECT monthly_cap_micros FROM orgs WHERE id = ?", (oid,)) == 1200 * M
    assert core.db.one("SELECT 1 AS x FROM messages m JOIN agents a ON a.id = m.recipient_id WHERE a.role = 'auditor' "
                       "AND m.body LIKE 'Review executed allocation%'")

    # The rest to the treasury, but the CFO disagrees: it escalates to the platform CEO, who decides.
    t = capital.propose(core.db, core.settings, agent=ceo, amount_micros=200 * M, to_org_id=None, **kw)
    with pytest.raises(PolicyRefusal) as e:  # surplus is fully claimed
        capital.propose(core.db, core.settings, agent=ceo, amount_micros=1 * M, to_org_id=oid, **kw)
    assert e.value.rule == "capital.surplus"
    capital.review(core.db, core.settings, agent=cfo, allocation_id=t["id"], approve=False, score=3, reason="keep cash")
    assert core.db.scalar("SELECT status FROM allocations WHERE id = ?", (t["id"],)) == "escalated"
    pceo = platform(core)["platform_ceo"]
    capital.resolve(core.db, core.settings, agent=pceo, allocation_id=t["id"], approve=True, reason="portfolio needs it")
    assert capital.treasury_balance(core.db) == 200 * M
    assert core.db.scalar("SELECT status FROM allocation_periods WHERE id = ?", (p["id"],)) == "decided"

    capital.record_outcome(core.db, agent=ceo, allocation_id=a["id"], actual_result="+$280 MRR", score=8)
    with pytest.raises(PolicyRefusal):
        capital.record_outcome(core.db, agent=team["sales"], allocation_id=a["id"], actual_result="x", score=1)


def test_written_case_needed_from_both_keys(core):
    oid = make_org(core, "Winner")
    book_month(core, oid, revenue_usd=1000, cost_usd=100)
    capital.capital_cycle(core.db, core.settings, at=AT)
    p = core.db.one("SELECT * FROM allocation_periods WHERE org_id = ? AND month = '2026-07'", (oid,))
    team = agents(core, oid)
    a = capital.propose(core.db, core.settings, agent=team["ceo"], scope="org", period_id=p["id"], to_org_id=None,
                        amount_micros=p["surplus_micros"], reason="market saturated", expected_result="fund new ventures",
                        case="TAM exhausted; extra spend shows no return in 3 tests")
    with pytest.raises(PolicyRefusal, match="case"):
        capital.review(core.db, core.settings, agent=team["cfo"], allocation_id=a["id"], approve=True, score=7, reason="ok")
    capital.review(core.db, core.settings, agent=team["cfo"], allocation_id=a["id"], approve=True, score=7, reason="ok",
                   case="agree: last 3 reinvestments returned < 1x")
    assert capital.treasury_balance(core.db) == p["surplus_micros"]


def test_treasury_guardrails(core):
    rich = make_org(core, "Winner")
    book_month(core, rich, revenue_usd=3000, cost_usd=100)
    loser = make_org(core, "Loser")
    book_month(core, loser, revenue_usd=10, cost_usd=200)
    trial = make_org(core, "Trial", cap_usd=1000, status="trial")
    capital.capital_cycle(core.db, core.settings, at=AT)
    p = core.db.one("SELECT * FROM allocation_periods WHERE org_id = ? AND month = '2026-07'", (rich,))
    lp = core.db.one("SELECT * FROM allocation_periods WHERE org_id = ? AND month = '2026-07'", (loser,))
    assert lp["verified_profit_micros"] < 0 and lp["surplus_micros"] == 0
    team = agents(core, rich)
    a = capital.propose(core.db, core.settings, agent=team["ceo"], scope="org", period_id=p["id"], to_org_id=None,
                        amount_micros=p["surplus_micros"] // 2, reason="r", expected_result="e")
    capital.review(core.db, core.settings, agent=team["cfo"], allocation_id=a["id"], approve=True, score=7, reason="ok")
    treasury = capital.treasury_balance(core.db)
    assert treasury > 0
    plat = platform(core)
    pceo, pcfo = plat["platform_ceo"], plat["platform_cfo"]
    kw = dict(scope="treasury", reason="r", expected_result="e")

    with pytest.raises(PolicyRefusal) as e:  # the floor is never spent
        capital.propose(core.db, core.settings, agent=pceo, to_org_id=rich, amount_micros=treasury - 50 * M, **kw)
    assert e.value.rule == "capital.treasury_floor"
    with pytest.raises(PolicyRefusal) as e:  # no org gets more than 40% of the month's treasury
        capital.propose(core.db, core.settings, agent=pceo, to_org_id=rich, amount_micros=treasury // 2, **kw)
    assert e.value.rule == "capital.concentration"
    with pytest.raises(PolicyRefusal) as e:  # trial orgs get at most their venture cap ($50)
        capital.propose(core.db, core.settings, agent=pceo, to_org_id=trial, amount_micros=60 * M, **kw)
    assert e.value.rule == "capital.trial_cap"
    with pytest.raises(PolicyRefusal) as e:  # a losing org needs a written case
        capital.propose(core.db, core.settings, agent=pceo, to_org_id=loser, amount_micros=10 * M, **kw)
    assert e.value.rule == "capital.losing_org"
    with pytest.raises(PolicyRefusal) as e:  # and nothing comes from its own (non-existent) surplus
        capital.propose(core.db, core.settings, agent=agents(core, loser)["ceo"], scope="org", period_id=lp["id"],
                        to_org_id=loser, amount_micros=1 * M, reason="r", expected_result="e")
    assert e.value.rule == "capital.no_profit"

    ok = capital.propose(core.db, core.settings, agent=pceo, to_org_id=trial, amount_micros=40 * M, **kw)
    capital.review(core.db, core.settings, agent=pcfo, allocation_id=ok["id"], approve=True, score=6, reason="cheap test")
    assert capital.treasury_balance(core.db) == treasury - 40 * M


async def test_tools_go_through_the_gateway(core):
    from bizops.gateway import invoke
    from bizops.tools import Ctx
    oid = make_org(core, "Winner")
    book_month(core, oid, revenue_usd=1000, cost_usd=100)
    capital.capital_cycle(core.db, core.settings, at=AT)
    team = agents(core, oid)
    st = await invoke(Ctx(core, team["ceo"]), "allocation_status", {})
    period = __import__("json").loads(st.content)["periods"][0]
    r = await invoke(Ctx(core, team["ceo"]), "propose_allocation",
                     {"period_id": period["period_id"], "to": "self", "amount_usd": 100, "reason": "r",
                      "expected_result": "e"})
    assert r.outcome == "ok"
    r = await invoke(Ctx(core, team["sales"]), "review_allocation",
                     {"allocation_id": "x", "approve": True, "score": 1, "reason": "r"})
    assert r.outcome == "refused"  # no 'capital' grant


def test_money_page_shows_waterfall(core):
    from fastapi.testclient import TestClient

    from bizops.web.app import create_app
    oid = make_org(core, "Winner")
    book_month(core, oid, revenue_usd=1000, cost_usd=100)
    capital.capital_cycle(core.db, core.settings, at=AT)
    with TestClient(create_app(core, run_scheduler=False)) as c:
        html = c.get("/money").text
    assert "Platform treasury" in html and "2026-07" in html and "Verified profit" in html

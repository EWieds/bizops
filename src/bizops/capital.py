"""Capital allocation: money is allocated by policy, not moved by hand.

Each complete month, once its refund/chargeback holdback has passed, the accountant job computes each
org's VERIFIED profit: cleared revenue (net of refunds and disputes) minus everything the org spent.
Only verified profit is allocatable. The waterfall, in order:

  1. Tax reserve: tax_reserve_pct of verified profit, held back, untouchable by agents.
  2. Operating reserve: topped up until it holds operating_reserve_months x the org's monthly costs.
  3. Allocatable surplus: the rest. The org CEO proposes how to split it between reinvesting in the org
     itself and the platform treasury; the platform CEO proposes where treasury money goes.

Two keys: the CEO proposes, the CFO scores it against evidence and approves or rejects. A rejection goes
to the platform CEO. Executed allocations go to the outside auditor, and each is later scored against what
actually happened. Reinvesting raises the recipient's monthly budget cap (these are internal budget
allocations; real cash stays in the owner's accounts). Owner distributions are not possible here: money
leaves the platform only when the owner moves it.

Guardrails enforced in this module (never by prompt): see `check_proposal`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from . import ledger
from .budget import check_allocation
from .config import Settings
from .db import Database, insert
from .engine import PolicyRefusal, get_org, post_message
from .events import log_event
from .util import fmt_usd, iso, new_id, now, parse_iso, usd_to_micros

DEFAULTS = {"tax_reserve_pct": 25, "operating_reserve_months": 2, "min_reinvest_self_pct": 50,
            "max_treasury_share_pct": 40, "max_cap_increase_pct": 25, "treasury_floor_usd": 100,
            "outcome_review_days": 60}


def cfg(settings: Settings) -> dict:
    return {**DEFAULTS, **(settings.policy.get("capital") or {})}


# ---------------------------------------------------------------- months

def month_bounds(month: str) -> tuple[str, str]:
    y, m = (int(x) for x in month.split("-"))
    start = datetime(y, m, 1, tzinfo=timezone.utc)
    end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=timezone.utc)
    return iso(start), iso(end)


def month_of(ts: datetime) -> str:
    return f"{ts.year:04d}-{ts.month:02d}"


def prev_month(month: str) -> str:
    y, m = (int(x) for x in month.split("-"))
    return f"{y - (m == 1):04d}-{(m - 2) % 12 + 1:02d}"


def closable_months(settings: Settings, at: datetime | None = None, back: int = 3) -> list[str]:
    """Complete months whose holdback has passed (every payment made in them has cleared), newest first."""
    at = at or now()
    holdback = timedelta(days=float(settings.policy.get("sales", {}).get("revenue_holdback_days", 30)))
    out, m = [], prev_month(month_of(at))
    for _ in range(back + 3):
        if parse_iso(month_bounds(m)[1]) + holdback <= at:
            out.append(m)
            if len(out) >= back:
                break
        m = prev_month(m)
    return out


# ---------------------------------------------------------------- the waterfall

def compute_period(db: Database, settings: Settings, org_id: str, month: str) -> dict:
    """Close one org-month (idempotent): verified profit -> tax reserve -> operating reserve -> surplus."""
    existing = db.one("SELECT * FROM allocation_periods WHERE month = ? AND org_id = ?", (month, org_id))
    if existing:
        return existing
    from .business import cleared_revenue_cents
    c = cfg(settings)
    start, end = month_bounds(month)
    cleared = cleared_revenue_cents(db, org_id=org_id, paid_from=start, paid_before=end) * 10_000
    costs = ledger.spent(db, since=start, org_id=org_id) - ledger.spent(db, since=end, org_id=org_id)
    # Card purchases come out of the wallet rather than the ops budget, but they are real costs.
    costs += ledger.purchases_since(db, org_id, start) - ledger.purchases_since(db, org_id, end)
    profit = cleared - costs
    tax = reserve = surplus = 0
    if profit > 0:
        tax = profit * int(c["tax_reserve_pct"]) // 100
        after_tax = profit - tax
        held = int(db.scalar("SELECT COALESCE(SUM(operating_reserve_micros), 0) AS s FROM allocation_periods "
                             "WHERE org_id = ?", (org_id,)) or 0)
        target = costs * int(c["operating_reserve_months"])
        reserve = min(after_tax, max(0, target - held))
        surplus = after_tax - reserve
    row = {"id": new_id("aper"), "month": month, "org_id": org_id, "cleared_revenue_micros": cleared,
           "costs_micros": costs, "verified_profit_micros": profit, "tax_reserve_micros": tax,
           "operating_reserve_micros": reserve, "surplus_micros": surplus,
           "status": "open" if surplus > 0 else "decided", "created_at": iso()}
    with db.tx() as conn:
        insert(conn, "allocation_periods", row)
        log_event(conn, actor_kind="system", kind="money", action="capital.period", org_id=org_id,
                  args={"month": month}, result={k: v for k, v in row.items() if k.endswith("_micros")},
                  outcome="ok", ref_type="allocation_period", ref_id=row["id"])
    return row


def treasury_balance(db: Database) -> int:
    return int(db.scalar(
        "SELECT COALESCE(SUM(CASE WHEN to_org_id IS NULL THEN amount_micros WHEN from_org_id IS NULL THEN -amount_micros "
        "ELSE 0 END), 0) AS s FROM allocations WHERE status = 'executed'") or 0)


def tax_reserve_total(db: Database) -> int:
    return int(db.scalar("SELECT COALESCE(SUM(tax_reserve_micros), 0) AS s FROM allocation_periods") or 0)


def period_remaining(db: Database, period: dict) -> int:
    """Surplus of a period not yet claimed by a live (proposed, escalated or executed) allocation."""
    used = int(db.scalar("SELECT COALESCE(SUM(amount_micros), 0) AS s FROM allocations WHERE period_id = ? "
                         "AND status IN ('proposed','escalated','executed')", (period["id"],)) or 0)
    return period["surplus_micros"] - used


def _cap_raised_this_month(db: Database, org_id: str) -> int:
    start = month_bounds(month_of(now()))[0]
    return int(db.scalar("SELECT COALESCE(SUM(amount_micros), 0) AS s FROM allocations WHERE to_org_id = ? "
                         "AND status = 'executed' AND executed_at >= ?", (org_id, start)) or 0)


def _latest_profit(db: Database, org_id: str) -> int | None:
    v = db.scalar("SELECT verified_profit_micros FROM allocation_periods WHERE org_id = ? ORDER BY month DESC LIMIT 1",
                  (org_id,))
    return None if v is None else int(v)


def check_proposal(db: Database, settings: Settings, a: dict, *, case: str = "") -> None:
    """Every guardrail, checked on proposal and again right before execution. Raises PolicyRefusal."""
    c = cfg(settings)
    amount = a["amount_micros"]
    if amount <= 0:
        raise PolicyRefusal("capital.amount", "amount must be positive")
    if a["scope"] == "org":
        period = db.one("SELECT * FROM allocation_periods WHERE id = ?", (a["period_id"],))
        if not period or period["org_id"] != a["from_org_id"]:
            raise PolicyRefusal("capital.period", "unknown period for this org")
        if period["verified_profit_micros"] <= 0 or period["surplus_micros"] <= 0:
            raise PolicyRefusal("capital.no_profit", "allocations come only from verified profit; this month has none")
        others = int(db.scalar("SELECT COALESCE(SUM(amount_micros), 0) AS s FROM allocations WHERE period_id = ? "
                               "AND status IN ('proposed','escalated','executed') AND id != ?", (period["id"], a["id"])) or 0)
        if others + amount > period["surplus_micros"]:
            raise PolicyRefusal("capital.surplus", f"only {fmt_usd(period['surplus_micros'] - others)} of this month's "
                                                   "surplus is unallocated")
        if a["to_org_id"] is None:
            to_treasury = int(db.scalar("SELECT COALESCE(SUM(amount_micros), 0) AS s FROM allocations WHERE period_id = ? "
                                        "AND to_org_id IS NULL AND status IN ('proposed','escalated','executed') AND id != ?",
                                        (period["id"], a["id"])) or 0) + amount
            limit = period["surplus_micros"] * (100 - int(c["min_reinvest_self_pct"])) // 100
            if to_treasury > limit and not case:
                raise PolicyRefusal("capital.min_reinvest_self",
                                    f"at least {c['min_reinvest_self_pct']}% of the surplus is reinvested in the org unless "
                                    f"both keys write down a reason (max {fmt_usd(limit)} to the treasury)")
        elif a["to_org_id"] != a["from_org_id"]:
            raise PolicyRefusal("capital.route", "an org's surplus goes to itself or to the treasury")
    else:  # treasury -> org
        if a["to_org_id"] is None:
            raise PolicyRefusal("capital.route", "treasury allocations go to an org")
        balance = treasury_balance(db)
        floor = usd_to_micros(c["treasury_floor_usd"])
        pending = int(db.scalar("SELECT COALESCE(SUM(amount_micros), 0) AS s FROM allocations WHERE from_org_id IS NULL "
                                "AND scope = 'treasury' AND status IN ('proposed','escalated') AND id != ?", (a["id"],)) or 0)
        if balance - pending - amount < floor:
            raise PolicyRefusal("capital.treasury_floor", f"the treasury floor ({fmt_usd(floor)}) is never spent; "
                                                          f"available {fmt_usd(max(0, balance - pending - floor))}")
        start = month_bounds(month_of(now()))[0]
        out_month = int(db.scalar("SELECT COALESCE(SUM(amount_micros), 0) AS s FROM allocations WHERE from_org_id IS NULL "
                                  "AND status = 'executed' AND executed_at >= ?", (start,)) or 0)
        to_org = int(db.scalar("SELECT COALESCE(SUM(amount_micros), 0) AS s FROM allocations WHERE from_org_id IS NULL "
                               "AND to_org_id = ? AND status = 'executed' AND executed_at >= ? AND id != ?",
                               (a["to_org_id"], start, a["id"])) or 0)
        month_treasury = balance + out_month
        if to_org + amount > month_treasury * int(c["max_treasury_share_pct"]) // 100:
            raise PolicyRefusal("capital.concentration", f"no org receives more than {c['max_treasury_share_pct']}% "
                                                         "of a month's treasury")
        recipient = get_org(db, a["to_org_id"])
        if recipient and recipient["status"] == "trial":
            venture_cap = usd_to_micros(settings.policy.get("venture", {}).get("starting_cap_usd", 50))
            got = int(db.scalar("SELECT COALESCE(SUM(amount_micros), 0) AS s FROM allocations WHERE from_org_id IS NULL "
                                "AND to_org_id = ? AND status = 'executed'", (a["to_org_id"],)) or 0)
            if got + amount > venture_cap:
                raise PolicyRefusal("capital.trial_cap", f"a trial org gets at most its venture cap ({fmt_usd(venture_cap)})")
        latest = _latest_profit(db, a["to_org_id"])
        if latest is not None and latest < 0 and not case:
            raise PolicyRefusal("capital.losing_org", "funding a losing org from the treasury needs a written case "
                                                      "from both keys")
    if a["to_org_id"]:  # the recipient's cap goes up by the amount: at most 25% a month, inside the platform cap
        org = get_org(db, a["to_org_id"])
        if not org or org["status"] == "retired":
            raise PolicyRefusal("capital.recipient", "unknown or retired org")
        raised = _cap_raised_this_month(db, org["id"])
        base = org["monthly_cap_micros"] - raised
        limit = max(base, 0) * int(c["max_cap_increase_pct"]) // 100
        if raised + amount > limit:
            raise PolicyRefusal("capital.cap_increase", f"an org's cap rises at most {c['max_cap_increase_pct']}% per "
                                                        f"month (room left {fmt_usd(max(0, limit - raised))})")
        chk = check_allocation(db, settings, new_org_cap=amount)
        if not chk.ok:
            raise PolicyRefusal(chk.rule, chk.detail)


# ---------------------------------------------------------------- two keys

def keyholders(db: Database, org_id: str | None) -> tuple[dict | None, dict | None]:
    """(CEO, CFO) for an org's own surplus, or the platform CEO and CFO for the treasury. An org without
    its own CFO uses the platform CFO as the second key."""
    root = db.scalar("SELECT id FROM orgs WHERE parent_org_id IS NULL ORDER BY created_at LIMIT 1")
    roles = ("ceo", "cfo") if org_id else ("platform_ceo", "platform_cfo")
    oid = org_id or root

    def one(o, role):
        return db.one("SELECT * FROM agents WHERE org_id = ? AND role = ? AND status = 'active' ORDER BY depth LIMIT 1",
                      (o, role))
    ceo = one(oid, roles[0])
    cfo = one(oid, roles[1]) or one(root, "platform_cfo")
    return ceo, cfo


def propose(db: Database, settings: Settings, *, agent: dict, scope: str, amount_micros: int, to_org_id: str | None,
            reason: str, expected_result: str, period_id: str | None = None, case: str = "",
            numbers: dict | None = None) -> dict:
    if not reason.strip() or not expected_result.strip():
        raise PolicyRefusal("capital.reasoning", "write down the reasoning and the expected result")
    if scope == "org":
        period = db.one("SELECT * FROM allocation_periods WHERE id = ?", (period_id,))
        if not period:
            raise PolicyRefusal("capital.period", "unknown period")
        from_org = period["org_id"]
        ceo, cfo = keyholders(db, from_org)
        month = period["month"]
    elif scope == "treasury":
        from_org, period, month = None, None, month_of(now())
        ceo, cfo = keyholders(db, None)
    else:
        raise PolicyRefusal("capital.scope", "scope is 'org' or 'treasury'")
    if not ceo or agent["id"] != ceo["id"]:
        raise PolicyRefusal("capital.key", "only the CEO of the money's owner proposes (first key)")
    ts = iso()
    a = {"id": new_id("alloc"), "period_id": period_id if scope == "org" else None, "month": month, "scope": scope,
         "from_org_id": from_org, "to_org_id": to_org_id, "amount_micros": int(amount_micros), "reason": reason.strip(),
         "numbers_json": json.dumps({**(numbers or {}), **({"case": case} if case else {})}),
         "expected_result": expected_result.strip(), "proposed_by": agent["id"], "status": "proposed",
         "created_at": ts, "updated_at": ts}
    check_proposal(db, settings, a, case=case)
    with db.tx() as conn:
        insert(conn, "allocations", a)
        log_event(conn, actor_kind="agent", kind="money", action="capital.propose", org_id=from_org or agent["org_id"],
                  agent_id=agent["id"], args={k: a[k] for k in ("scope", "amount_micros", "to_org_id", "reason")},
                  outcome="ok", ref_type="allocation", ref_id=a["id"])
        if cfo:
            post_message(db, settings, org_id=cfo["org_id"], sender_kind="agent", sender_id=agent["id"],
                         recipient_kind="agent", recipient_id=cfo["id"], type="instruction", conn=conn,
                         body=f"Second key needed: allocation {a['id']} proposes {fmt_usd(a['amount_micros'])} "
                              f"{'from ' + (from_org or 'the treasury')} to {to_org_id or 'the platform treasury'}. "
                              f"Reason: {a['reason']} Expected: {a['expected_result']} Score it against the evidence "
                              "(return on past reinvestment, payback period, risk, concentration) and approve or reject it "
                              "with review_allocation.")
    return a


def review(db: Database, settings: Settings, *, agent: dict, allocation_id: str, approve: bool, score: float,
           reason: str, case: str = "") -> dict:
    """The second key. Approve executes (guardrails re-checked); reject escalates to the platform CEO."""
    a = db.one("SELECT * FROM allocations WHERE id = ?", (allocation_id,))
    if not a or a["status"] != "proposed":
        raise PolicyRefusal("capital.state", "no such open proposal")
    _, cfo = keyholders(db, a["from_org_id"])
    if not cfo or agent["id"] != cfo["id"]:
        raise PolicyRefusal("capital.key", "only the CFO for this money reviews it (second key)")
    if not reason.strip():
        raise PolicyRefusal("capital.reasoning", "write down why")
    db.execute("UPDATE allocations SET reviewed_by = ?, cfo_score = ?, cfo_reason = ?, updated_at = ? WHERE id = ?",
               (agent["id"], float(score), reason.strip(), iso(), a["id"]))
    if not approve:
        return _escalate(db, settings, a, f"CFO rejected: {reason}")
    needs_case = bool(json.loads(a["numbers_json"] or "{}").get("case"))
    if needs_case and not case.strip():
        raise PolicyRefusal("capital.case", "this allocation relies on a written case; both keys must write one "
                                            "(pass case=...) to approve it")
    return execute(db, settings, a["id"], actor=agent, case=case or json.loads(a["numbers_json"] or "{}").get("case", ""))


def _escalate(db: Database, settings: Settings, a: dict, why: str) -> dict:
    db.execute("UPDATE allocations SET status = 'escalated', updated_at = ? WHERE id = ?", (iso(), a["id"]))
    pceo, _ = keyholders(db, None)
    log_event(db, actor_kind="system", kind="money", action="capital.escalate", org_id=a["from_org_id"] or a["to_org_id"],
              args={"allocation": a["id"]}, result={"why": why}, outcome="ok", ref_type="allocation", ref_id=a["id"])
    if pceo:
        post_message(db, settings, org_id=pceo["org_id"], sender_kind="system", sender_id=None, recipient_kind="agent",
                     recipient_id=pceo["id"], type="instruction",
                     body=f"Allocation {a['id']} ({fmt_usd(a['amount_micros'])} to {a['to_org_id'] or 'the treasury'}) is "
                          f"disputed between the keys: {why}. Decide it with resolve_allocation.")
    return {**a, "status": "escalated"}


def resolve(db: Database, settings: Settings, *, agent: dict, allocation_id: str, approve: bool, reason: str) -> dict:
    """The platform CEO settles a disagreement between the keys. The guardrails still apply."""
    a = db.one("SELECT * FROM allocations WHERE id = ?", (allocation_id,))
    if not a or a["status"] != "escalated":
        raise PolicyRefusal("capital.state", "no such escalated allocation")
    pceo, _ = keyholders(db, None)
    if not pceo or agent["id"] != pceo["id"]:
        raise PolicyRefusal("capital.key", "only the platform CEO resolves disagreements")
    if a["scope"] == "treasury":
        raise PolicyRefusal("capital.key", "the platform CEO proposed this; a rejected treasury allocation stays rejected")
    if not approve:
        db.execute("UPDATE allocations SET status = 'rejected', actual_result = ?, updated_at = ? WHERE id = ?",
                   (f"rejected by platform CEO: {reason}", iso(), a["id"]))
        return {**a, "status": "rejected"}
    return execute(db, settings, a["id"], actor=agent, case=json.loads(a["numbers_json"] or "{}").get("case", ""),
                   allowed=("escalated",))


def execute(db: Database, settings: Settings, allocation_id: str, *, actor: dict, case: str = "",
            allowed: tuple[str, ...] = ("proposed",)) -> dict:
    a = db.one("SELECT * FROM allocations WHERE id = ?", (allocation_id,))
    if not a or a["status"] not in allowed:
        raise PolicyRefusal("capital.state", "allocation is not open")
    check_proposal(db, settings, a, case=case)
    ts = iso()
    with db.tx() as conn:
        conn.execute("UPDATE allocations SET status = 'executed', executed_at = ?, updated_at = ? WHERE id = ?",
                     (ts, ts, a["id"]))
        if a["to_org_id"]:
            conn.execute("UPDATE orgs SET monthly_cap_micros = monthly_cap_micros + ?, updated_at = ? WHERE id = ?",
                         (a["amount_micros"], ts, a["to_org_id"]))
        if a["period_id"]:
            left = conn.execute("SELECT p.surplus_micros - COALESCE((SELECT SUM(amount_micros) FROM allocations x "
                                "WHERE x.period_id = p.id AND x.status = 'executed'), 0) FROM allocation_periods p "
                                "WHERE p.id = ?", (a["period_id"],)).fetchone()[0]
            if left <= 0:
                conn.execute("UPDATE allocation_periods SET status = 'decided' WHERE id = ?", (a["period_id"],))
        log_event(conn, actor_kind="agent", kind="money", action="capital.execute", org_id=a["to_org_id"] or a["from_org_id"],
                  agent_id=actor["id"], args={"allocation": a["id"], "amount_micros": a["amount_micros"],
                                              "to": a["to_org_id"] or "treasury"}, outcome="ok",
                  ref_type="allocation", ref_id=a["id"])
    auditor = db.one("SELECT * FROM agents WHERE role = 'auditor' AND status = 'active' LIMIT 1")
    if auditor:
        post_message(db, settings, org_id=auditor["org_id"], sender_kind="system", sender_id=None, recipient_kind="agent",
                     recipient_id=auditor["id"], type="instruction",
                     body=f"Review executed allocation {a['id']}: {fmt_usd(a['amount_micros'])} "
                          f"from {a['from_org_id'] or 'treasury'} to {a['to_org_id'] or 'treasury'}. Reason: {a['reason']}. "
                          "Check the numbers against the ledger and file a finding if anything is off.")
    return {**a, "status": "executed", "executed_at": ts}


def record_outcome(db: Database, *, agent: dict, allocation_id: str, actual_result: str, score: float) -> dict:
    a = db.one("SELECT * FROM allocations WHERE id = ?", (allocation_id,))
    if not a or a["status"] != "executed":
        raise PolicyRefusal("capital.state", "only executed allocations are scored")
    if agent["id"] not in (a["proposed_by"], a["reviewed_by"]):
        raise PolicyRefusal("capital.key", "the keys that made the call score it")
    db.execute("UPDATE allocations SET actual_result = ?, outcome_score = ?, updated_at = ? WHERE id = ?",
               (actual_result.strip(), float(score), iso(), a["id"]))
    log_event(db, actor_kind="agent", kind="money", action="capital.outcome", agent_id=agent["id"],
              org_id=a["to_org_id"] or a["from_org_id"], args={"allocation": a["id"], "score": score}, outcome="ok",
              ref_type="allocation", ref_id=a["id"])
    return {**a, "actual_result": actual_result, "outcome_score": score}


# ---------------------------------------------------------------- the monthly job

def capital_cycle(db: Database, settings: Settings, at: datetime | None = None) -> int:
    """Close any closable months for every org, ask the keys to allocate surplus, and remind them to score
    old decisions. Returns the number of periods closed."""
    n = 0
    orgs = db.all("SELECT * FROM orgs WHERE status IN ('active','trial') AND parent_org_id IS NOT NULL")
    for month in reversed(closable_months(settings, at)):
        start, end = month_bounds(month)
        for org in orgs:
            if org["created_at"] >= end or db.one("SELECT 1 AS x FROM allocation_periods WHERE month = ? AND org_id = ?",
                                                  (month, org["id"])):
                continue
            p = compute_period(db, settings, org["id"], month)
            n += 1
            if p["surplus_micros"] > 0:
                ceo, _ = keyholders(db, org["id"])
                if ceo:
                    c = cfg(settings)
                    post_message(db, settings, org_id=org["id"], sender_kind="system", sender_id=None, recipient_kind="agent",
                                 recipient_id=ceo["id"], type="instruction",
                                 body=f"{month} is closed. Verified profit {fmt_usd(p['verified_profit_micros'])}: tax reserve "
                                      f"{fmt_usd(p['tax_reserve_micros'])}, operating reserve {fmt_usd(p['operating_reserve_micros'])}, "
                                      f"allocatable surplus {fmt_usd(p['surplus_micros'])} (period {p['id']}). Propose how to split "
                                      f"it with propose_allocation: reinvest in your org (at least {c['min_reinvest_self_pct']}% "
                                      "unless you and your CFO write down why not) and/or send it to the platform treasury. "
                                      "Base it on evidence: what past reinvestment returned, payback, risk.")
    c = cfg(settings)
    old = iso((at or now()) - timedelta(days=int(c["outcome_review_days"])))
    for a in db.all("SELECT * FROM allocations WHERE status = 'executed' AND outcome_score IS NULL AND executed_at <= ?",
                    (old,)):
        key = f"capital:outcome_reminded:{a['id']}"
        from .engine import get_setting, set_setting
        if get_setting(db, key):
            continue
        set_setting(db, key, iso())
        post_message(db, settings, org_id=db.scalar("SELECT org_id FROM agents WHERE id = ?", (a["proposed_by"],)),
                     sender_kind="system", sender_id=None, recipient_kind="agent", recipient_id=a["proposed_by"],
                     type="instruction",
                     body=f"Score allocation {a['id']} ({fmt_usd(a['amount_micros'])}, expected: {a['expected_result']}) "
                          "against what actually happened, with record_allocation_outcome. Use measured numbers.")
    return n


def waterfall_view(db: Database) -> dict:
    periods = db.all("SELECT p.*, o.name AS org_name FROM allocation_periods p JOIN orgs o ON o.id = p.org_id "
                     "ORDER BY p.month DESC, o.name LIMIT 60")
    for p in periods:
        p["allocated"] = int(db.scalar("SELECT COALESCE(SUM(amount_micros), 0) AS s FROM allocations WHERE period_id = ? "
                                       "AND status = 'executed'", (p["id"],)) or 0)
        p["reinvested"] = int(db.scalar("SELECT COALESCE(SUM(amount_micros), 0) AS s FROM allocations WHERE period_id = ? "
                                        "AND status = 'executed' AND to_org_id IS NOT NULL", (p["id"],)) or 0)
    allocations = db.all("SELECT a.*, f.name AS from_name, t.name AS to_name, pa.name AS proposer, ra.name AS reviewer "
                         "FROM allocations a LEFT JOIN orgs f ON f.id = a.from_org_id LEFT JOIN orgs t ON t.id = a.to_org_id "
                         "LEFT JOIN agents pa ON pa.id = a.proposed_by LEFT JOIN agents ra ON ra.id = a.reviewed_by "
                         "ORDER BY a.created_at DESC LIMIT 50")
    return {"treasury": treasury_balance(db), "tax_reserve": tax_reserve_total(db),
            "operating_reserve": int(db.scalar("SELECT COALESCE(SUM(operating_reserve_micros), 0) AS s "
                                               "FROM allocation_periods") or 0),
            "periods": periods, "allocations": allocations}

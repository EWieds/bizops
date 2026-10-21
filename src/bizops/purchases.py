"""Purchases: how agents buy software, subscriptions and services on their own.

  1. An agent asks (request_purchase): vendor, item, price, once or monthly, why, and the task it serves.
     Refused in code if it's over the per-purchase limit or more than the wallet has left after other
     pending requests. The wallet is what the owner loaded; agents can never add to it.
  2. The platform's Chief Investment Officer decides (decide_purchase), or the owner on the Money page.
     Nobody approves their own request. On approval the amount leaves the wallet at once (ledger kind
     'purchase') and a virtual card is issued for exactly that amount, locked to the vendor (cards.py).
  3. The agent checks out in the browser. The card number is typed by the browser, never shown to it.
  4. Monthly purchases are debited from the wallet again every 30 days. If the wallet can't cover the
     next month the card is paused (the vendor's next charge is declined) until the owner adds money.

Purchases draw only on the wallet, not on the model/ops budget, so buying a tool never starves the
agents of model time.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from . import ledger
from .config import Settings
from .db import Database, insert
from .engine import PolicyRefusal, get_agent, get_org, org_ceo, post_message, root_org
from .events import log_event
from .util import fmt_usd, iso, new_id, now, parse_iso, usd_to_micros

PERIODS = ("once", "monthly")
MONTH = timedelta(days=30)


def _cfg(settings: Settings) -> dict[str, Any]:
    return settings.policy.get("purchases", {})


def cio(db: Database) -> dict | None:
    """The platform's Chief Investment Officer: the agent who decides purchase requests."""
    root = root_org(db)
    if not root:
        return None
    return db.one("SELECT * FROM agents WHERE org_id = ? AND role = 'platform_cfo' AND status = 'active' "
                  "ORDER BY created_at LIMIT 1", (root["id"],))


def get(db: Database, purchase_id: str) -> dict | None:
    return db.one("SELECT * FROM purchases WHERE id = ?", (purchase_id,))


def available(db: Database, org_id: str, *, exclude: str | None = None) -> int:
    """Wallet balance minus what pending requests would take."""
    pending = int(db.scalar("SELECT COALESCE(SUM(amount_micros),0) FROM purchases WHERE org_id = ? "
                            "AND status = 'pending' AND id != ?", (org_id, exclude or "")) or 0)
    return ledger.wallet_balance(db, org_id) - pending


def monthly_commitments(db: Database, org_id: str) -> int:
    return int(db.scalar("SELECT COALESCE(SUM(amount_micros),0) FROM purchases WHERE org_id = ? "
                         "AND period = 'monthly' AND status IN ('approved','paused')", (org_id,)) or 0)


def _notify(db: Database, settings: Settings, agent: dict | None, body: str, type: str = "status") -> None:
    if agent and agent["status"] == "active":
        post_message(db, settings, org_id=agent["org_id"], sender_kind="system", sender_id=None,
                     recipient_kind="agent", recipient_id=agent["id"], type=type, body=body)


def _check_limits(db: Database, settings: Settings, *, org_id: str, amount: int, period: str,
                  exclude: str | None = None) -> None:
    cfg = _cfg(settings)
    if amount <= 0:
        raise PolicyRefusal("purchase.amount", "amount must be positive")
    max_one = usd_to_micros(cfg.get("max_per_purchase_usd", 300))
    if amount > max_one:
        raise PolicyRefusal("purchase.max", f"max {fmt_usd(max_one)} per purchase (one-time price or monthly price)")
    if period == "monthly":
        cap = usd_to_micros(cfg.get("max_monthly_subscriptions_usd_per_org", 300))
        current = monthly_commitments(db, org_id)
        if current + amount > cap:
            raise PolicyRefusal("purchase.subscriptions_max",
                                f"subscriptions would total {fmt_usd(current + amount)}/month; the limit is {fmt_usd(cap)}")
    left = available(db, org_id, exclude=exclude)
    if amount > left:
        raise PolicyRefusal("purchase.wallet", f"the wallet has {fmt_usd(max(left, 0))} available; the owner must "
                                               "load more money on the Money page")


def request(db: Database, settings: Settings, *, agent: dict, vendor: str, item: str, amount_usd: float,
            period: str, reason: str, vendor_url: str = "", task_id: str | None = None) -> dict:
    if period not in PERIODS:
        raise PolicyRefusal("purchase.period", "period must be 'once' or 'monthly'")
    if not (vendor.strip() and item.strip() and reason.strip()):
        raise PolicyRefusal("purchase.fields", "vendor, item and reason are required")
    try:
        amount = usd_to_micros(amount_usd)
    except (TypeError, ValueError):
        raise PolicyRefusal("purchase.amount", "amount_usd must be a number")
    org_id = agent["org_id"]
    _check_limits(db, settings, org_id=org_id, amount=amount, period=period)
    pid = new_id("pur")
    row = {"id": pid, "org_id": org_id, "agent_id": agent["id"], "task_id": task_id, "vendor": vendor.strip(),
           "vendor_url": vendor_url.strip(), "item": item.strip(), "amount_micros": amount, "period": period,
           "reason": reason.strip(), "status": "pending", "decided_by": None, "decision_reason": None,
           "decided_at": None, "card_provider": None, "card_token": None, "card_last4": None,
           "next_charge_at": None, "created_at": iso(), "updated_at": iso()}
    with db.tx() as conn:
        insert(conn, "purchases", row)
        log_event(conn, actor_kind="agent", kind="purchase", action="purchase.request", org_id=org_id,
                  agent_id=agent["id"], args={"vendor": vendor, "item": item, "amount_usd": amount_usd, "period": period},
                  outcome="ok", ref_type="purchase", ref_id=pid)
    org = get_org(db, org_id)
    per = "/month" if period == "monthly" else " one-time"
    _notify(db, settings, cio(db),
            f"Purchase request [{pid}] from {agent['name']} ({org['name'] if org else org_id}): {item} from {vendor} "
            f"for {fmt_usd(amount)}{per}. Why: {reason}. Wallet available after this: "
            f"{fmt_usd(available(db, org_id))}. Decide with decide_purchase (approve or deny, with your reason).",
            type="question")
    return row


async def decide(core: Any, *, purchase_id: str, decider: str, approve: bool, reason: str) -> dict:
    """decider: the deciding agent's id, or 'human' for the owner."""
    db, settings = core.db, core.settings
    p = get(db, purchase_id)
    if not p:
        raise PolicyRefusal("purchase.unknown", f"no purchase {purchase_id}")
    if decider == p["agent_id"]:
        raise PolicyRefusal("purchase.self_approval", "nobody decides their own purchase request")
    if not reason.strip():
        raise PolicyRefusal("purchase.reason", "give the reason for your decision")
    requester = get_agent(db, p["agent_id"])
    actor = "human" if decider == "human" else "agent"
    if not approve:
        with db.tx() as conn:
            n = conn.execute("UPDATE purchases SET status = 'denied', decided_by = ?, decision_reason = ?, decided_at = ?, "
                             "updated_at = ? WHERE id = ? AND status = 'pending'",
                             (decider, reason, iso(), iso(), purchase_id)).rowcount
            if not n:
                raise PolicyRefusal("purchase.state", f"purchase is {p['status']}, not pending")
            log_event(conn, actor_kind=actor, kind="purchase", action="purchase.deny", org_id=p["org_id"],
                      agent_id=None if decider == "human" else decider, args={"reason": reason}, outcome="ok",
                      ref_type="purchase", ref_id=purchase_id)
        _notify(db, settings, requester, f"Purchase [{purchase_id}] ({p['item']} from {p['vendor']}) was DENIED: {reason}")
        return get(db, purchase_id)

    with db.tx() as conn:
        status = conn.execute("SELECT status FROM purchases WHERE id = ?", (purchase_id,)).fetchone()[0]
        if status != "pending":
            raise PolicyRefusal("purchase.state", f"purchase is {status}, not pending")
        _check_limits(db, settings, org_id=p["org_id"], amount=p["amount_micros"], period=p["period"], exclude=purchase_id)
        conn.execute("UPDATE purchases SET status = 'approved', decided_by = ?, decision_reason = ?, decided_at = ?, "
                     "updated_at = ? WHERE id = ?", (decider, reason, iso(), iso(), purchase_id))
        ledger.record(conn, org_id=p["org_id"], agent_id=p["agent_id"], kind="purchase", amount_micros=p["amount_micros"],
                      source="provider", memo=f"{p['vendor']}: {p['item']}", ref_id=f"{purchase_id}:1")
        log_event(conn, actor_kind=actor, kind="purchase", action="purchase.approve", org_id=p["org_id"],
                  agent_id=None if decider == "human" else decider,
                  args={"reason": reason, "amount": fmt_usd(p["amount_micros"])}, outcome="ok",
                  ref_type="purchase", ref_id=purchase_id)
    cards = core.cards
    try:
        card = await cards.create(memo=f"bizops {p['vendor']} {purchase_id}", limit_cents=p["amount_micros"] // 10_000,
                                  period=p["period"])
    except Exception as e:
        with db.tx() as conn:
            conn.execute("UPDATE purchases SET status = 'failed', decision_reason = ?, updated_at = ? WHERE id = ?",
                         (f"{reason} | card issue failed: {str(e)[:200]}", iso(), purchase_id))
            ledger.record(conn, org_id=p["org_id"], agent_id=p["agent_id"], kind="purchase_refund",
                          amount_micros=p["amount_micros"], source="provider", memo=f"card failed: {str(e)[:120]}",
                          ref_id=f"{purchase_id}:1")
        _notify(db, settings, requester, f"Purchase [{purchase_id}] was approved but the card could not be issued "
                                         f"({str(e)[:200]}). The money went back to the wallet.")
        raise
    nxt = iso(now() + MONTH) if p["period"] == "monthly" else None
    db.execute("UPDATE purchases SET card_provider = ?, card_token = ?, card_last4 = ?, next_charge_at = ?, updated_at = ? "
               "WHERE id = ?", (cards.name, card["token"], card.get("last4", ""), nxt, iso(), purchase_id))
    test = "" if cards.live else " (TEST card: no real money; checkouts will decline it)"
    _notify(db, settings, requester,
            f"Purchase [{purchase_id}] APPROVED: {p['item']} from {p['vendor']}, {fmt_usd(p['amount_micros'])}"
            f"{'/month' if p['period'] == 'monthly' else ''}. Card ending {card.get('last4', '')}{test}, limited to that "
            f"amount and locked to the first merchant it is used at. Check out with the browser: in browser_type use "
            f"{{{{card:{purchase_id}:number}}}}, {{{{card:{purchase_id}:exp}}}} (MM/YY), {{{{card:{purchase_id}:cvv}}}} "
            f"(also :exp_month, :exp_year, :name, :zip). Approval note: {reason}", type="instruction")
    return get(db, purchase_id)


async def cancel(core: Any, *, purchase_id: str, by: str, org_id: str | None = None) -> dict:
    """Stop a purchase: close its card. Money already charged for the current period is not refunded."""
    db = core.db
    p = get(db, purchase_id)
    if not p or (org_id and p["org_id"] != org_id):
        raise PolicyRefusal("purchase.unknown", f"no purchase {purchase_id} in your org")
    if p["status"] not in ("pending", "approved", "paused"):
        raise PolicyRefusal("purchase.state", f"purchase is already {p['status']}")
    if p["card_token"]:
        await core.cards.set_state(p["card_token"], "CLOSED")
    db.execute("UPDATE purchases SET status = 'cancelled', next_charge_at = NULL, updated_at = ? WHERE id = ?",
               (iso(), purchase_id))
    log_event(db, actor_kind="human" if by == "human" else "agent", kind="purchase", action="purchase.cancel",
              org_id=p["org_id"], agent_id=None if by == "human" else by, outcome="ok",
              ref_type="purchase", ref_id=purchase_id)
    return get(db, purchase_id)


async def card_values(core: Any, *, org_id: str, purchase_id: str) -> dict[str, str]:
    """What the browser types for {{card:...}} placeholders. Only for an approved purchase of the same org."""
    p = get(core.db, purchase_id)
    if not p or p["org_id"] != org_id:
        raise PolicyRefusal("purchase.unknown", f"no purchase {purchase_id} in your org")
    if p["status"] != "approved" or not p["card_token"]:
        raise PolicyRefusal("purchase.state", f"purchase {purchase_id} is {p['status']}; only approved purchases have a card")
    d = await core.cards.details(p["card_token"])
    yy = d["exp_year"][-2:]
    return {"number": d["number"], "cvv": d["cvv"], "exp_month": d["exp_month"], "exp_year": d["exp_year"],
            "exp": f"{d['exp_month']}/{yy}", "name": core.settings.secret("CARDHOLDER_NAME") or "",
            "zip": core.settings.secret("CARDHOLDER_ZIP") or ""}


async def renew_due(core: Any) -> int:
    """Monthly purchases: debit the wallet for the next 30 days, or pause the card if it can't."""
    db, settings = core.db, core.settings
    n = 0
    for p in db.all("SELECT * FROM purchases WHERE period = 'monthly' AND status IN ('approved','paused') "
                    "AND next_charge_at IS NOT NULL AND next_charge_at <= ?", (iso(),)):
        due = parse_iso(p["next_charge_at"]) or now()
        if ledger.wallet_balance(db, p["org_id"]) >= p["amount_micros"]:
            k = 1 + int(db.scalar("SELECT COUNT(*) FROM ledger_entries WHERE kind = 'purchase' AND ref_id LIKE ?",
                                  (f"{p['id']}:%",)) or 0)
            with db.tx() as conn:
                ledger.record(conn, org_id=p["org_id"], agent_id=p["agent_id"], kind="purchase",
                              amount_micros=p["amount_micros"], source="provider",
                              memo=f"{p['vendor']}: {p['item']} (month {k})", ref_id=f"{p['id']}:{k}")
                conn.execute("UPDATE purchases SET status = 'approved', next_charge_at = ?, updated_at = ? WHERE id = ?",
                             (iso(due + MONTH), iso(), p["id"]))
            if p["status"] == "paused" and p["card_token"]:
                await core.cards.set_state(p["card_token"], "OPEN")
            n += 1
        elif p["status"] == "approved":
            if p["card_token"]:
                await core.cards.set_state(p["card_token"], "PAUSED")
            db.execute("UPDATE purchases SET status = 'paused', updated_at = ? WHERE id = ?", (iso(), p["id"]))
            log_event(db, actor_kind="system", kind="purchase", action="purchase.pause", org_id=p["org_id"],
                      result={"reason": "wallet can't cover the next month"}, outcome="ok",
                      ref_type="purchase", ref_id=p["id"])
            msg = (f"Subscription [{p['id']}] {p['item']} from {p['vendor']} ({fmt_usd(p['amount_micros'])}/month) is "
                   "PAUSED: the wallet can't cover next month, so the card will decline the renewal. It resumes "
                   "automatically when the owner loads more money. Cancel it with cancel_purchase if it isn't worth it.")
            _notify(db, settings, org_ceo(db, p["org_id"]), msg)
            _notify(db, settings, cio(db), msg)
            n += 1
    return n

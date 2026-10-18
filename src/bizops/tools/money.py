"""Money tools. See payments.py for the safety model."""

from __future__ import annotations

from .. import budget, ledger
from ..engine import PolicyRefusal, get_org
from ..util import day_start, fmt_usd, usd_to_micros
from . import Ctx, ToolOutput, tool


def _payee(ctx: Ctx, payee_id: str) -> dict:
    p = ctx.db.one("SELECT * FROM payees WHERE id = ? AND (org_id IS NULL OR org_id = ?)",
                   (payee_id, ctx.agent["org_id"]))
    if not p:
        raise PolicyRefusal("pay.payee", "unknown payee for your org; use request_payee")
    if p["status"] != "active":
        raise PolicyRefusal("pay.payee_inactive", "payee is disabled")
    return p


def _amount(args: dict) -> int:
    try:
        return usd_to_micros(args["amount_usd"])
    except (KeyError, TypeError, ValueError):
        raise PolicyRefusal("pay.amount", "amount_usd must be a number")


def _key(args: dict, amount: int) -> str:
    return str(args.get("idempotency_key") or f"{args.get('task_id')}:{args.get('payee_id')}:{amount}")


def _estimate(ctx: Ctx, args: dict) -> int:
    """Budget pre-check amount. A retry of an already-made payment costs nothing new."""
    amount = _amount(args)
    dup = ctx.db.one("SELECT 1 AS x FROM ledger_entries WHERE org_id = ? AND kind = 'payment' AND ref_id = ?",
                     (ctx.agent["org_id"], _key(args, amount)))
    return 0 if dup else amount


@tool("wallet_balance", grant="money", action_class="internal",
      description="Your org's wallet balance, today's payments and the payment limits.")
async def wallet_balance(ctx: Ctx, args: dict) -> dict:
    m = ctx.settings.policy["money"]
    org = ctx.agent["org_id"]
    return {"balance": fmt_usd(ledger.wallet_balance(ctx.db, org)),
            "paid_today": fmt_usd(ledger.payments_since(ctx.db, org, day_start())),
            "per_payment_max": f"${m['per_payment_max_usd']}",
            "daily_max": f"${m['daily_payments_max_usd_per_org']}"}


@tool("list_payees", grant="money", action_class="internal",
      description="Payees the owner has allowlisted for your org.")
async def list_payees(ctx: Ctx, args: dict) -> list:
    rows = ctx.db.all("SELECT id, name, kind, max_per_payment_micros, notes FROM payees "
                      "WHERE status = 'active' AND (org_id IS NULL OR org_id = ?)", (ctx.agent["org_id"],))
    for r in rows:
        r["max_per_payment"] = fmt_usd(r.pop("max_per_payment_micros")) if r["max_per_payment_micros"] else "policy max"
    return rows


@tool("pay", grant="money", action_class="spend", timeout_s=60,
      cost_estimate=_estimate,
      description="Pay an allowlisted payee from your org wallet. Needs a memo and the task it serves. "
                  "Limited per payment, per day, by wallet balance and budget; screened by the approver.",
      properties={"payee_id": {"type": "string"}, "amount_usd": {"type": "number"},
                  "memo": {"type": "string", "description": "what this buys"},
                  "task_id": {"type": "string"},
                  "idempotency_key": {"type": "string", "description": "reuse to avoid double payment"}},
      required=["payee_id", "amount_usd", "memo", "task_id"])
async def pay(ctx: Ctx, args: dict) -> ToolOutput:
    db, settings, agent = ctx.db, ctx.settings, ctx.agent
    m = settings.policy["money"]
    amount = _amount(args)
    payee = _payee(ctx, args["payee_id"])
    key = _key(args, amount)
    org_id = agent["org_id"]
    with db.tx() as conn:
        dup = conn.execute("SELECT id FROM ledger_entries WHERE org_id = ? AND kind = 'payment' AND ref_id = ?",
                           (org_id, key)).fetchone()
        if dup:
            return ToolOutput({"status": "duplicate", "ledger_id": dup[0], "note": "already paid with this key"},
                              record_ledger=False)
        if amount <= 0:
            raise PolicyRefusal("pay.amount", "amount must be positive")
        if amount > usd_to_micros(m["per_payment_max_usd"]):
            raise PolicyRefusal("pay.per_payment_max", f"max ${m['per_payment_max_usd']} per payment")
        if payee["max_per_payment_micros"] and amount > payee["max_per_payment_micros"]:
            raise PolicyRefusal("pay.payee_max", f"max {fmt_usd(payee['max_per_payment_micros'])} for this payee")
        today = ledger.payments_since(conn, org_id, day_start())
        if today + amount > usd_to_micros(m["daily_payments_max_usd_per_org"]):
            raise PolicyRefusal("pay.daily_max", f"org daily payment limit ${m['daily_payments_max_usd_per_org']}")
        balance = ledger.wallet_balance(conn, org_id)
        if amount > balance:
            raise PolicyRefusal("pay.balance", f"wallet has {fmt_usd(balance)}; the owner must deposit more")
        chk = budget.check_spend(conn, settings, org=get_org(db, org_id), agent=agent, amount=amount)
        if not chk.ok:
            raise PolicyRefusal(chk.rule, chk.detail)
        lid = ledger.record(conn, org_id=org_id, agent_id=agent["id"], run_id=ctx.run_id, kind="payment",
                            amount_micros=amount, source="provider",
                            memo=f"{payee['name']}: {args['memo']}", ref_id=key)
    try:
        receipt = await ctx.core.payments.send(payee=payee, amount_micros=amount, memo=args["memo"],
                                               idempotency_key=key)
    except Exception as e:
        ledger.record(db, org_id=org_id, agent_id=agent["id"], run_id=ctx.run_id, kind="payment_reversal",
                      amount_micros=amount, source="provider", memo=f"failed: {e}", ref_id=key)
        raise
    return ToolOutput({"status": receipt.get("status"), "paid": fmt_usd(amount), "payee": payee["name"],
                       "receipt": receipt, "ledger_id": lid},
                      cost_micros=amount, record_ledger=False, ref_type="ledger", ref_id=lid)


@tool("request_payee", grant="money", action_class="new_payee",
      description="Ask the owner to allowlist a new payee (vendor). Goes to the owner's queue; continue other work.",
      properties={"name": {"type": "string"}, "details": {"type": "string", "description": "who, what for, how to pay"},
                  "expected_monthly_usd": {"type": "number"}, "task_id": {"type": "string"}},
      required=["name", "details"])
async def request_payee(ctx: Ctx, args: dict) -> str:  # never runs: always a hard stop
    raise PolicyRefusal("hard_stop", "unreachable")

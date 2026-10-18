"""Capital allocation tools (two keys). The rules live in capital.py and are enforced in code."""

from __future__ import annotations

from .. import capital
from ..util import fmt_usd, usd_to_micros
from . import Ctx, tool


def _amount(args: dict) -> int:
    return usd_to_micros(float(args["amount_usd"]))


@tool("allocation_status", grant="capital", action_class="internal",
      description="The capital-allocation waterfall: your org's closed months (verified profit, tax and operating "
                  "reserves, allocatable surplus and what is left of it), the platform treasury, open proposals and "
                  "the guardrails.")
async def allocation_status(ctx: Ctx, args: dict) -> dict:
    db, org = ctx.db, ctx.agent["org_id"]
    platform = ctx.agent["role"].startswith("platform_") or ctx.agent["role"] == "auditor"
    where = "" if platform else "WHERE org_id = ?"
    periods = db.all(f"SELECT * FROM allocation_periods {where} ORDER BY month DESC LIMIT 12", () if platform else (org,))
    out = [{"period_id": p["id"], "month": p["month"], "org_id": p["org_id"],
            "cleared_revenue": fmt_usd(p["cleared_revenue_micros"]), "costs": fmt_usd(p["costs_micros"]),
            "verified_profit": fmt_usd(p["verified_profit_micros"]), "tax_reserve": fmt_usd(p["tax_reserve_micros"]),
            "operating_reserve": fmt_usd(p["operating_reserve_micros"]), "surplus": fmt_usd(p["surplus_micros"]),
            "unallocated": fmt_usd(capital.period_remaining(db, p))} for p in periods]
    allocs = db.all("SELECT id, scope, from_org_id, to_org_id, amount_micros, status, reason, expected_result, cfo_score, "
                    "actual_result, outcome_score FROM allocations " + ("" if platform else "WHERE from_org_id = ? OR to_org_id = ? ")
                    + "ORDER BY created_at DESC LIMIT 20", () if platform else (org, org))
    for a in allocs:
        a["amount"] = fmt_usd(a.pop("amount_micros"))
    return {"periods": out, "allocations": allocs, "treasury": fmt_usd(capital.treasury_balance(db)),
            "rules": {k: v for k, v in capital.cfg(ctx.settings).items()}}


@tool("propose_allocation", grant="capital", action_class="internal", ref_type="allocation",
      description="First key (CEO). Org CEO: split a closed month's surplus (period_id) between reinvesting in your org "
                  "(to='self') and the platform treasury (to='treasury'). Platform CEO: move treasury money to an org "
                  "(scope treasury, to=<org id>). Reinvesting raises the recipient's monthly budget cap. Your CFO must "
                  "approve. Write the reasoning, the numbers you used and the expected result; 'case' is the written case "
                  "needed to send more than the default share to the treasury or to fund a losing org.",
      properties={"period_id": {"type": "string", "description": "for an org's own surplus"},
                  "to": {"type": "string", "description": "'self', 'treasury', or an org id (treasury scope)"},
                  "amount_usd": {"type": "number"}, "reason": {"type": "string"},
                  "expected_result": {"type": "string", "description": "what this should achieve, measurably, and by when"},
                  "numbers": {"type": "object", "description": "the figures your decision used"},
                  "case": {"type": "string"}},
      required=["to", "amount_usd", "reason", "expected_result"])
async def propose_allocation(ctx: Ctx, args: dict) -> dict:
    to = args["to"]
    if args.get("period_id"):
        period = ctx.db.one("SELECT * FROM allocation_periods WHERE id = ?", (args["period_id"],))
        to_org = None if to == "treasury" else (period["org_id"] if period and to == "self" else to)
        scope = "org"
    else:
        scope, to_org = "treasury", (None if to in ("treasury", "self") else to)
    a = capital.propose(ctx.db, ctx.settings, agent=ctx.agent, scope=scope, amount_micros=_amount(args), to_org_id=to_org,
                        reason=args["reason"], expected_result=args["expected_result"], period_id=args.get("period_id"),
                        case=args.get("case", ""), numbers=args.get("numbers"))
    return {"allocation_id": a["id"], "status": a["status"], "next": "waiting for the CFO's review (second key)"}


@tool("review_allocation", grant="capital", action_class="internal", ref_type="allocation",
      description="Second key (CFO). Score a proposed allocation 0-10 against the evidence (return on past reinvestment, "
                  "payback period, risk, concentration) and approve (executes it) or reject (goes to the platform CEO). "
                  "If the proposal relies on a written case, approving requires your own 'case' too.",
      properties={"allocation_id": {"type": "string"}, "approve": {"type": "boolean"}, "score": {"type": "number"},
                  "reason": {"type": "string"}, "case": {"type": "string"}},
      required=["allocation_id", "approve", "score", "reason"])
async def review_allocation(ctx: Ctx, args: dict) -> dict:
    a = capital.review(ctx.db, ctx.settings, agent=ctx.agent, allocation_id=args["allocation_id"], approve=bool(args["approve"]),
                       score=float(args["score"]), reason=args["reason"], case=args.get("case", ""))
    return {"allocation_id": a["id"], "status": a["status"]}


@tool("resolve_allocation", grant="capital", action_class="internal", ref_type="allocation",
      description="Platform CEO: decide an allocation the CEO and CFO of a business disagreed on. Guardrails still apply.",
      properties={"allocation_id": {"type": "string"}, "approve": {"type": "boolean"}, "reason": {"type": "string"}},
      required=["allocation_id", "approve", "reason"])
async def resolve_allocation(ctx: Ctx, args: dict) -> dict:
    a = capital.resolve(ctx.db, ctx.settings, agent=ctx.agent, allocation_id=args["allocation_id"],
                        approve=bool(args["approve"]), reason=args["reason"])
    return {"allocation_id": a["id"], "status": a["status"]}


@tool("record_allocation_outcome", grant="capital", action_class="internal", ref_type="allocation",
      description="Score an executed allocation you proposed or approved against what actually happened (0-10), using "
                  "measured numbers, so future calls get better.",
      properties={"allocation_id": {"type": "string"}, "actual_result": {"type": "string"}, "score": {"type": "number"}},
      required=["allocation_id", "actual_result", "score"])
async def record_allocation_outcome(ctx: Ctx, args: dict) -> dict:
    a = capital.record_outcome(ctx.db, agent=ctx.agent, allocation_id=args["allocation_id"],
                               actual_result=args["actual_result"], score=float(args["score"]))
    return {"allocation_id": a["id"], "outcome_score": a["outcome_score"]}

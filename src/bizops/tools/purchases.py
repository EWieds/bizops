"""Purchase tools. See purchases.py for the flow and the limits."""

from __future__ import annotations

from .. import purchases
from ..engine import PolicyRefusal
from ..util import fmt_usd
from . import Ctx, ToolOutput, tool


def _row(p: dict) -> dict:
    return {"id": p["id"], "vendor": p["vendor"], "item": p["item"], "amount": fmt_usd(p["amount_micros"]),
            "period": p["period"], "status": p["status"], "card_last4": p["card_last4"],
            "next_charge_at": p["next_charge_at"], "decision": p["decision_reason"], "requested_by": p["agent_id"]}


@tool("request_purchase", grant="money", action_class="internal", ref_type="purchase",
      description="Ask to buy software, a subscription or a service with a card. The platform's Chief Investment "
                  "Officer approves or denies it; you get a message either way. If approved, a virtual card for "
                  "exactly that amount is issued, and you check out with the browser. Include tax in amount_usd. "
                  "Limited by the wallet the owner loaded and per-purchase limits. Continue other work meanwhile.",
      properties={"vendor": {"type": "string", "description": "company you are buying from"},
                  "vendor_url": {"type": "string", "description": "pricing or checkout page"},
                  "item": {"type": "string", "description": "exactly what you are buying (plan name)"},
                  "amount_usd": {"type": "number", "description": "total charge per period, incl. tax"},
                  "period": {"type": "string", "enum": list(purchases.PERIODS)},
                  "why": {"type": "string", "description": "what it unlocks, expected return, cheaper alternatives considered"},
                  "task_id": {"type": "string"}},
      required=["vendor", "item", "amount_usd", "period", "why"])
async def request_purchase(ctx: Ctx, args: dict) -> ToolOutput:
    p = purchases.request(ctx.db, ctx.settings, agent=ctx.agent, vendor=str(args["vendor"]), item=str(args["item"]),
                          amount_usd=args["amount_usd"], period=str(args["period"]), reason=str(args["why"]),
                          vendor_url=str(args.get("vendor_url") or ""), task_id=args.get("task_id"))
    who = "the Chief Investment Officer" if purchases.cio(ctx.db) else "the owner (no CIO is active)"
    return ToolOutput({"purchase_id": p["id"], "status": "pending", "decided_by": who,
                       "note": "You'll get a message with the decision. Don't wait: continue other work."},
                      ref_type="purchase", ref_id=p["id"])


@tool("decide_purchase", grant="approve_purchases", action_class="internal", ref_type="purchase", timeout_s=60,
      description="Approve or deny a purchase request. Approve only purchases with a clear expected return for "
                  "their business, at a fair price, with no cheaper adequate option. Approval moves the money out "
                  "of that business's wallet and issues a card for exactly that amount.",
      properties={"purchase_id": {"type": "string"},
                  "decision": {"type": "string", "enum": ["approve", "deny"]},
                  "why": {"type": "string", "description": "your reasoning, with the numbers (shown to the requester)"}},
      required=["purchase_id", "decision", "why"])
async def decide_purchase(ctx: Ctx, args: dict) -> ToolOutput:
    p = await purchases.decide(ctx.core, purchase_id=str(args["purchase_id"]), decider=ctx.agent["id"],
                               approve=args["decision"] == "approve", reason=str(args["why"]))
    return ToolOutput(_row(p), ref_type="purchase", ref_id=p["id"])


@tool("pending_purchases", grant="approve_purchases", action_class="internal",
      description="Purchase requests waiting for your decision, across all businesses, with each wallet's balance.")
async def pending_purchases(ctx: Ctx, args: dict) -> list:
    rows = ctx.db.all("SELECT p.*, o.name AS org_name FROM purchases p JOIN orgs o ON o.id = p.org_id "
                      "WHERE p.status = 'pending' ORDER BY p.created_at")
    return [{**_row(p), "business": p["org_name"], "why": p["reason"], "vendor_url": p["vendor_url"],
             "wallet_available": fmt_usd(purchases.available(ctx.db, p["org_id"], exclude=p["id"]))} for p in rows]


@tool("list_purchases", grant="money", action_class="internal",
      description="Your business's purchases and subscriptions: status, card, next monthly charge.")
async def list_purchases(ctx: Ctx, args: dict) -> list:
    rows = ctx.db.all("SELECT * FROM purchases WHERE org_id = ? ORDER BY created_at DESC LIMIT 50", (ctx.agent["org_id"],))
    return [_row(p) for p in rows]


@tool("cancel_purchase", grant="money", action_class="internal", ref_type="purchase",
      description="Cancel a purchase or subscription you no longer need: its card is closed so the vendor can't "
                  "charge it again. Cancel the subscription at the vendor too.",
      properties={"purchase_id": {"type": "string"}}, required=["purchase_id"])
async def cancel_purchase(ctx: Ctx, args: dict) -> ToolOutput:
    p = await purchases.cancel(ctx.core, purchase_id=str(args["purchase_id"]), by=ctx.agent["id"],
                               org_id=ctx.agent["org_id"])
    if not p:
        raise PolicyRefusal("purchase.unknown", "no such purchase")
    return ToolOutput(_row(p), ref_type="purchase", ref_id=p["id"])

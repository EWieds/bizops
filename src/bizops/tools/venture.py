"""Launching new businesses from the platform, inside the venture budget set by the owner."""

from __future__ import annotations

from ..engine import PolicyRefusal, create_org_from_template, org_ceo
from ..util import month_start
from . import Ctx, ToolOutput, tool


@tool("create_business", grant="venture", action_class="internal", ref_type="org", timeout_s=60,
      description="Launch a new business (org) from a template with a thorough brief: thesis, target customer, "
                  "offer, evidence, 60-day plan, kill criteria. It starts on trial, inside the venture budget.",
      properties={"name": {"type": "string"},
                  "template": {"type": "string", "description": "generic, voice-agent-sales, rank-and-rent, digital-products"},
                  "brief": {"type": "string"}, "monthly_cap_usd": {"type": "number"}},
      required=["name", "template", "brief"])
async def create_business(ctx: Ctx, args: dict) -> ToolOutput:
    v = ctx.settings.policy.get("venture", {})
    if ctx.agent["org_id"] != (ctx.db.scalar("SELECT id FROM orgs WHERE parent_org_id IS NULL ORDER BY created_at LIMIT 1")):
        raise PolicyRefusal("venture.scope", "only the platform launches businesses")
    if args["template"] not in v.get("templates", []):
        raise PolicyRefusal("venture.template", f"template must be one of {v.get('templates')}")
    if len(args["brief"].strip()) < 200:
        raise PolicyRefusal("venture.brief", "write a thorough brief (thesis, customer, offer, evidence, plan, kill criteria)")
    made = int(ctx.db.scalar("SELECT COUNT(*) AS n FROM events WHERE action = 'create_business' AND phase = 'after' "
                             "AND outcome = 'ok' AND ts >= ?", (month_start(),)) or 0)
    if made >= int(v.get("max_new_orgs_per_month", 2)):
        raise PolicyRefusal("venture.monthly", "venture budget: max new businesses this month reached")
    trial = int(ctx.db.scalar("SELECT COUNT(*) AS n FROM orgs WHERE status = 'trial'") or 0)
    if trial >= int(v.get("max_on_trial", 3)):
        raise PolicyRefusal("venture.trial", "too many businesses on trial; review or close one first")
    cap = min(float(args.get("monthly_cap_usd") or v.get("starting_cap_usd", 50)), float(v.get("starting_cap_usd", 50)))
    root = ctx.agent["org_id"]
    oid = create_org_from_template(ctx.db, ctx.settings, template=args["template"], name=args["name"], brief=args["brief"],
                                   parent_org_id=root, supervisor_agent_id=ctx.agent["id"], created_by=ctx.agent["id"],
                                   monthly_cap_usd=cap)
    ctx.db.execute("UPDATE orgs SET status = 'trial' WHERE id = ?", (oid,))
    ceo = org_ceo(ctx.db, oid)
    return ToolOutput({"org_id": oid, "ceo": ceo["id"] if ceo else None, "monthly_cap_usd": cap, "status": "trial"},
                      ref_type="org", ref_id=oid)

"""Goal tools: set, update, list. Measured values come only from connectors or the owner."""

from __future__ import annotations

from ..goals import GOAL_STATUSES, goals_for, org_goals, set_goal, update_goal
from . import Ctx, ToolOutput, tool


@tool("set_goal", grant="goals", action_class="internal", ref_type="goal",
      description="Set a measurable goal for yourself or an agent below you (OKR style). Cascade org goals to "
                  "reports with parent_goal_id. Max 5 active goals per agent.",
      properties={"owner": {"type": "string", "description": "agent id (yourself or a report)"},
                  "title": {"type": "string", "description": "the objective, in plain words"},
                  "metric": {"type": "string", "description": "what is measured, e.g. 'qualified leads/week'"},
                  "target": {"type": "number"}, "unit": {"type": "string"},
                  "due": {"type": "string", "description": "YYYY-MM-DD or number of days from now"},
                  "parent_goal_id": {"type": "string"},
                  "review_every_days": {"type": "number", "description": "review cadence, default 3"},
                  "metric_key": {"type": "string", "description": "optional: measured automatically from real data. One of: "
                                 "revenue_usd, customers, mrr_usd, leads, emails_sent, replies, live_voice_agents"}},
      required=["owner", "title", "metric", "target", "due"])
async def set_goal_tool(ctx: Ctx, args: dict) -> ToolOutput:
    g = set_goal(ctx.db, ctx.settings, setter=ctx.agent, owner_id=args["owner"], title=args["title"],
                 metric=args["metric"], target=args.get("target"), unit=args.get("unit", ""), due=args.get("due"),
                 parent_goal_id=args.get("parent_goal_id"), review_every_days=args.get("review_every_days") or 3,
                 metric_key=args.get("metric_key") or None)
    return ToolOutput({"goal_id": g["id"], "due": g["due_at"][:10], "next_review": g["next_review_at"][:16]},
                      ref_type="goal", ref_id=g["id"])


@tool("update_goal", grant="goals", action_class="internal", ref_type="goal",
      description="Review a goal: set its status and a short progress note (what moved, evidence, next step). "
                  "You can't write the measured value; that comes from connectors or the owner.",
      properties={"goal_id": {"type": "string"},
                  "status": {"type": "string", "enum": list(GOAL_STATUSES)},
                  "progress_note": {"type": "string"}},
      required=["goal_id", "progress_note"])
async def update_goal_tool(ctx: Ctx, args: dict) -> ToolOutput:
    g = update_goal(ctx.db, actor=ctx.agent, goal_id=args["goal_id"], status=args.get("status"),
                    progress_note=args.get("progress_note"))
    return ToolOutput({"goal_id": g["id"], "status": g["status"], "next_review": g["next_review_at"][:16]},
                      ref_type="goal", ref_id=g["id"])


@tool("list_goals", grant="core", action_class="internal",
      description="Your active goals, or all goals in your org (scope='org').",
      properties={"scope": {"type": "string", "enum": ["mine", "org"]}})
async def list_goals(ctx: Ctx, args: dict) -> list:
    rows = org_goals(ctx.db, ctx.agent["org_id"]) if args.get("scope") == "org" else goals_for(ctx.db, ctx.agent)
    keep = ("id", "title", "metric", "target", "unit", "measured_value", "due_at", "status", "progress_note",
            "owner_name", "parent_goal_id")
    return [{k: r.get(k) for k in keep if k in r} for r in rows]

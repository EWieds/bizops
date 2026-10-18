"""Hiring and retiring. Limits and budget allocation are enforced in engine.spawn_agent."""

from __future__ import annotations

from ..engine import retire_agent, spawn_agent
from . import Ctx, ToolOutput, tool


@tool("spawn_agent", grant="hire", action_class="internal", ref_type="agent",
      description="Hire a new report under you from the role library (e.g. researcher, writer, coder, designer, "
                  "sales, marketer, accountant, hr). Its budget comes out of your own. Org headcount, depth and "
                  "reports-per-manager limits apply.",
      properties={"role": {"type": "string"}, "name": {"type": "string"},
                  "budget_usd": {"type": "number", "description": "monthly budget moved from your budget"},
                  "focus": {"type": "string", "description": "what this hire specializes in here"}},
      required=["role", "budget_usd", "focus"])
async def spawn_agent_tool(ctx: Ctx, args: dict) -> ToolOutput:
    row = spawn_agent(ctx.db, ctx.settings, hirer=ctx.agent, role=args["role"], name=args.get("name"),
                      budget_usd=float(args["budget_usd"]), focus=args.get("focus", ""))
    return ToolOutput({"agent_id": row["id"], "name": row["name"], "role": row["role"],
                       "budget": f"${row['budget_micros'] / 1e6:.2f}"}, ref_type="agent", ref_id=row["id"])


@tool("retire_agent", grant="hire", action_class="internal", ref_type="agent",
      description="Retire an agent below you whose KPI isn't moving. Its budget returns to its manager and its "
                  "open tasks, goals and reports move up one level.",
      properties={"agent_id": {"type": "string"}, "reason": {"type": "string"}},
      required=["agent_id", "reason"])
async def retire_agent_tool(ctx: Ctx, args: dict) -> ToolOutput:
    r = retire_agent(ctx.db, actor=ctx.agent, agent_id=args["agent_id"], reason=args["reason"])
    return ToolOutput({"retired": r["id"], "name": r["name"]}, ref_type="agent", ref_id=r["id"])

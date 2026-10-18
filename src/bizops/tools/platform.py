"""Read-only platform tools: lets the platform agents answer questions from the log and ledger."""

from __future__ import annotations

from .. import ledger
from ..util import fmt_usd, month_start
from . import Ctx, tool


@tool("org_overview", grant="platform_read", action_class="internal",
      description="All orgs with status, agent count, open/blocked tasks and month spend vs cap.")
async def org_overview(ctx: Ctx, args: dict) -> list:
    rows = ctx.db.all(
        "SELECT o.id, o.name, o.status, o.template, o.monthly_cap_micros, "
        "(SELECT COUNT(*) FROM agents a WHERE a.org_id = o.id AND a.status != 'retired') AS agents, "
        "(SELECT COUNT(*) FROM tasks t WHERE t.org_id = o.id AND t.status IN ('open','in_progress')) AS open_tasks, "
        "(SELECT COUNT(*) FROM tasks t WHERE t.org_id = o.id AND t.status = 'blocked') AS blocked_tasks "
        "FROM orgs o ORDER BY o.created_at")
    ms = month_start()
    for r in rows:
        r["month_spend"] = fmt_usd(ledger.spent(ctx.db, since=ms, org_id=r["id"]))
        r["cap"] = fmt_usd(r.pop("monthly_cap_micros"))
        r["wallet"] = fmt_usd(ledger.wallet_balance(ctx.db, r["id"]))
    return rows


@tool("query_activity", grant="platform_read", action_class="internal",
      description="Search the activity log (read-only). Filter by org_id, agent_id, action, outcome, event_id.",
      properties={"org_id": {"type": "string"}, "agent_id": {"type": "string"}, "action": {"type": "string"},
                  "outcome": {"type": "string"}, "event_id": {"type": "string"},
                  "since": {"type": "string", "description": "ISO timestamp"},
                  "limit": {"type": "integer"}})
async def query_activity(ctx: Ctx, args: dict) -> list:
    where, params = ["phase != 'before'"], []
    for col in ("org_id", "agent_id", "action", "outcome"):
        if args.get(col):
            where.append(f"{col} = ?")
            params.append(args[col])
    if args.get("event_id"):
        where.append("id = ?")
        params.append(args["event_id"])
    if args.get("since"):
        where.append("ts >= ?")
        params.append(args["since"])
    limit = max(1, min(int(args.get("limit") or 30), 100))
    return ctx.db.all(
        "SELECT id, ts, org_id, agent_id, action, outcome, reason, cost_micros, "
        "substr(COALESCE(result_json, ''), 1, 300) AS result FROM events "
        f"WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT {limit}", tuple(params))


@tool("ledger_summary", grant="platform_read", action_class="internal",
      description="Month-to-date spend by org and kind, plus wallet balances.")
async def ledger_summary(ctx: Ctx, args: dict) -> list:
    return ctx.db.all(
        "SELECT o.name AS org, l.kind, COUNT(*) AS n, SUM(l.amount_micros) AS micros "
        "FROM ledger_entries l JOIN orgs o ON o.id = l.org_id WHERE l.ts >= ? GROUP BY o.name, l.kind",
        (month_start(),))

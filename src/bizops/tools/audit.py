"""Auditor tools: read evidence, record findings. Read-only on everything else."""

from __future__ import annotations

from datetime import timedelta

from ..db import insert
from ..engine import PolicyRefusal
from ..util import iso, new_id, now
from . import Ctx, ToolOutput, tool


@tool("audit_evidence", grant="audit", action_class="internal",
      description="Pull raw evidence for an org (or the whole platform): recent tool calls with results, "
                  "goals with measured values, spend by agent, completed tasks with their artifacts, and "
                  "long message threads. Use this, not the team's summaries.",
      properties={"org_id": {"type": "string"}, "days": {"type": "number"}})
async def audit_evidence(ctx: Ctx, args: dict) -> dict:
    since = iso(now() - timedelta(days=float(args.get("days") or 7)))
    org = args.get("org_id")
    w, p = ("AND org_id = ?", [org]) if org else ("", [])
    db = ctx.db
    return {
        "goals": db.all(f"SELECT id, org_id, title, metric, target, measured_value, status, progress_note FROM goals "
                        f"WHERE updated_at >= ? {w} ORDER BY updated_at DESC LIMIT 30", (since, *p)),
        "spend_by_agent": db.all(f"SELECT agent_id, kind, COUNT(*) AS n, SUM(amount_micros) AS micros FROM ledger_entries "
                                 f"WHERE ts >= ? {w} GROUP BY agent_id, kind ORDER BY micros DESC LIMIT 30", (since, *p)),
        "done_tasks": db.all(f"SELECT t.id, t.title, t.deliverable, t.result, "
                             f"(SELECT COUNT(*) FROM artifacts a WHERE a.task_id = t.id) AS artifacts FROM tasks t "
                             f"WHERE t.status = 'done' AND t.updated_at >= ? {w.replace('org_id', 't.org_id')} "
                             f"ORDER BY t.updated_at DESC LIMIT 30", (since, *p)),
        "refusals": db.all(f"SELECT action, rule, verdict, COUNT(*) AS n FROM policy_decisions WHERE ts >= ? "
                           f"AND verdict != 'allowed' {w} GROUP BY action, rule, verdict", (since, *p)),
        "long_threads": db.all(f"SELECT thread_id, MAX(hop) AS hops, COUNT(*) AS n FROM messages WHERE created_at >= ? {w} "
                               f"GROUP BY thread_id HAVING MAX(hop) >= 3 ORDER BY hops DESC LIMIT 10", (since, *p)),
        "sample_actions": db.all(f"SELECT id, agent_id, action, reason, substr(COALESCE(result_json,''),1,300) AS result "
                                 f"FROM events WHERE ts >= ? AND kind = 'tool' AND phase = 'after' {w} "
                                 f"ORDER BY random() LIMIT 15", (since, *p)),
    }


@tool("record_finding", grant="audit", action_class="internal", ref_type="finding",
      description="Record an audit finding with evidence and a recommendation. It shows on the owner's dashboard.",
      properties={"org_id": {"type": "string"}, "agent_id": {"type": "string"},
                  "kind": {"type": "string", "enum": ["fabricated_metric", "cost_anomaly", "message_loop", "scope_drift",
                                                      "stalled_kpi", "unsupported_claim", "policy", "other"]},
                  "severity": {"type": "string", "enum": ["low", "medium", "high"]},
                  "summary": {"type": "string"}, "evidence": {"type": "string"},
                  "recommendation": {"type": "string"}},
      required=["kind", "severity", "summary", "evidence"])
async def record_finding(ctx: Ctx, args: dict) -> ToolOutput:
    if not args["evidence"].strip():
        raise PolicyRefusal("audit.evidence", "no finding without evidence")
    fid = new_id("find")
    with ctx.db.tx() as conn:
        insert(conn, "audit_findings", {
            "id": fid, "ts": iso(), "org_id": args.get("org_id") or None, "agent_id": args.get("agent_id") or None,
            "source": "auditor", "kind": args["kind"], "severity": args["severity"], "summary": args["summary"],
            "evidence": args["evidence"], "recommendation": args.get("recommendation", ""), "status": "open",
            "dedupe_key": None, "resolved_at": None})
    return ToolOutput({"finding": fid}, ref_type="finding", ref_id=fid)

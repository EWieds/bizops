"""Management tools: inspect your team's actual work (from the log, not their summaries) and send work for
review and improvement."""

from __future__ import annotations

from datetime import timedelta

from ..engine import PolicyRefusal, create_task, get_agent, is_descendant
from ..util import clip, fmt_usd, iso, now
from . import Ctx, ToolOutput, tool


def _subtree(ctx: Ctx) -> list[dict]:
    rows = ctx.db.all("SELECT * FROM agents WHERE org_id = ? AND status != 'retired' AND id != ?",
                      (ctx.agent["org_id"], ctx.agent["id"]))
    return [a for a in rows if is_descendant(ctx.db, a["id"], ctx.agent["id"])]


@tool("team_report", grant="comms", action_class="internal", timeout_s=60,
      description="Inspect what your team actually did (from the activity log, not their own summaries): per person "
                  "their recent work sessions, tasks done/open/blocked with results, files produced, emails sent, refused "
                  "actions and errors, spend and goals. Use it to find what needs work, then delegate.",
      properties={"agent_id": {"type": "string", "description": "one person (optional; default: everyone under you)"},
                  "hours": {"type": "number", "description": "look-back window, default 24"}})
async def team_report(ctx: Ctx, args: dict) -> dict:
    db = ctx.db
    since = iso(now() - timedelta(hours=float(args.get("hours") or 24)))
    team = _subtree(ctx)
    if args.get("agent_id"):
        team = [a for a in team if a["id"] == args["agent_id"]]
        if not team:
            raise PolicyRefusal("team.scope", "that agent isn't in your team")
    if not team:
        return {"team": [], "note": "you have no reports"}
    out = []
    for a in team:
        aid = a["id"]
        runs = db.all("SELECT status, wake_reason, summary, error, cost_micros, started_at FROM runs WHERE agent_id = ? "
                      "AND started_at >= ? ORDER BY started_at DESC LIMIT 5", (aid, since))
        tasks = db.all("SELECT id, title, status, deliverable, result, blocked_reason, updated_at FROM tasks WHERE assignee_id = ? "
                       "AND (updated_at >= ? OR status IN ('open','in_progress','blocked')) ORDER BY updated_at DESC LIMIT 10",
                       (aid, since))
        files = db.all("SELECT id, name, task_id, created_at FROM artifacts WHERE agent_id = ? AND created_at >= ? "
                       "ORDER BY created_at DESC LIMIT 10", (aid, since))
        pages = db.all("SELECT path, title, updated_at FROM sites WHERE created_by = ? AND updated_at >= ?", (aid, since))
        problems = db.all("SELECT action, outcome, substr(COALESCE(result_json,''),1,200) AS detail FROM events WHERE agent_id = ? "
                          "AND ts >= ? AND outcome IN ('refused','error','hard_stop') ORDER BY id DESC LIMIT 8", (aid, since))
        emails = db.scalar("SELECT COUNT(*) AS n FROM emails WHERE agent_id = ? AND direction = 'out' AND created_at >= ?",
                           (aid, since))
        spend = db.scalar("SELECT COALESCE(SUM(amount_micros),0) AS s FROM ledger_entries WHERE agent_id = ? AND ts >= ?",
                          (aid, since))
        goals = db.all("SELECT id, title, status, measured_value, target, progress_note FROM goals WHERE owner_agent_id = ? "
                       "AND status IN ('active','at_risk')", (aid,))
        out.append({
            "agent": {"id": aid, "name": a["name"], "role": a["role"], "status": a["status"]},
            "sessions": [{"status": r["status"], "why": r["wake_reason"], "summary": clip(r["summary"] or r["error"] or "", 300)}
                         for r in runs],
            "tasks": [{**t, "result": clip(t["result"] or "", 300)} for t in tasks],
            "files": files, "pages": pages, "emails_sent": emails, "spend": fmt_usd(spend),
            "problems": problems, "goals": goals,
        })
    return {"window_since": since, "team": out}


@tool("request_review", grant="comms", action_class="internal", ref_type="task",
      description="Send a piece of work (a file, a page or a finished task) to the right specialist on your team to "
                  "review and improve, e.g. ask the Head of Growth to tighten the landing page copy. Creates a task "
                  "for them with your specific feedback.",
      properties={"reviewer": {"type": "string", "description": "agent id (yourself or someone under you)"},
                  "artifact_id": {"type": "string"}, "page_path": {"type": "string"}, "task_id": {"type": "string"},
                  "feedback": {"type": "string", "description": "what's wrong or what 'better' means, specifically"}},
      required=["reviewer", "feedback"])
async def request_review(ctx: Ctx, args: dict) -> ToolOutput:
    db = ctx.db
    reviewer = get_agent(db, args["reviewer"])
    if not reviewer:
        raise PolicyRefusal("review.reviewer", "no such agent")
    what, ref = [], None
    if args.get("artifact_id"):
        a = db.one("SELECT id, name FROM artifacts WHERE (id = ? OR name = ?) AND org_id = ? ORDER BY created_at DESC LIMIT 1",
                   (args["artifact_id"], args["artifact_id"], ctx.agent["org_id"]))
        if not a:
            raise PolicyRefusal("review.missing", "no such file in your org")
        what.append(f"file {a['name']} ({a['id']}: read it with read_artifact, save the improved version with write_artifact)")
    if args.get("page_path"):
        p = db.one("SELECT path, title FROM sites WHERE org_id = ? AND path = ?", (ctx.agent["org_id"], args["page_path"].strip("/") or "index"))
        if not p:
            raise PolicyRefusal("review.missing", "no such page")
        what.append(f"web page /{p['path']} ('{p['title']}': republish the improved version with publish_page at the same path)")
    if args.get("task_id"):
        t = db.one("SELECT id, title, result FROM tasks WHERE id = ? AND org_id = ?", (args["task_id"], ctx.agent["org_id"]))
        if not t:
            raise PolicyRefusal("review.missing", "no such task")
        what.append(f"the work from task {t['id']} '{t['title']}' (result: {clip(t['result'] or '', 300)})")
        ref = t["id"]
    if not what:
        raise PolicyRefusal("review.nothing", "say what to review: artifact_id, page_path or task_id")
    task = create_task(db, creator=ctx.agent, assignee_id=reviewer["id"], parent_task_id=ref,
                       title=f"Review and improve: {clip('; '.join(what), 90)}",
                       deliverable="The improved version shipped (same place) plus a short note of what you changed and why",
                       description=f"Review {'; '.join(what)}.\nFeedback from {ctx.agent['name']}: {args['feedback']}\n"
                                   "Apply your expertise; fix anything else you find. If it's already excellent, say why and close the task.")
    return ToolOutput({"task_id": task["id"], "reviewer": reviewer["name"]}, ref_type="task", ref_id=task["id"])

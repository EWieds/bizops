"""Dashboard: FastAPI + Jinja + htmx (5s polling) + Tailwind/Chart.js from CDNs. No build step.

Bind to localhost (default). There is no auth: add it before exposing this anywhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import os
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from .. import business, ledger
from ..budget import platform_caps, spend_caps_enforced
from ..core import Core
from ..db import Database
from ..engine import (PolicyRefusal, create_org_from_template, get_agent, get_org, get_setting, human_message,
                      org_ceo, org_tree, platform_paused, root_org, set_paused)
from ..goals import goals_for, org_goals, record_measurement
from ..runtime import model_for, preview_context, system_prompt
from ..events import log_event
from ..gateway import invoke
from ..payments import create_payee, deposit
from ..scheduler import Scheduler
from ..tools import Ctx
from ..util import day_start, fmt_usd, iso, loads, month_start, now, parse_iso

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _ago(ts: str | None) -> str:
    dt = parse_iso(ts)
    if not dt:
        return "never"
    s = int((now() - dt).total_seconds())
    if s < 0:
        s = -s
        if s >= 86400:
            return f"in {s / 86400:.1f}d"
        if s >= 3600:
            return f"in {s / 3600:.1f}h"
        return f"in {s // 60}m" if s >= 60 else f"in {s}s"
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _pretty(s: str | None) -> str:
    v = loads(s, s)
    if isinstance(v, str):
        inner = loads(v, None)
        if isinstance(inner, (dict, list)):
            v = inner
        else:
            return v
    return json.dumps(v, indent=2, ensure_ascii=False, default=str)


def _num(v: Any) -> str:
    if v is None:
        return "-"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{f:,.0f}" if f == int(f) else f"{f:,.2f}"


def _dur(s: int | None) -> str:
    s = int(s or 0)
    if s >= 86400 and s % 86400 == 0:
        return f"{s // 86400}d"
    if s >= 3600:
        return f"{s / 3600:g}h"
    return f"{s // 60}m" if s >= 60 else f"{s}s"


TEMPLATES.env.filters.update(usd=fmt_usd, ago=_ago, pretty=_pretty, loads=lambda s: loads(s, {}),
                             short=lambda s: (s or "")[-6:], num=_num, dur=_dur)

BACKEND_LABELS = {"dry_run": "Dry run (scripted)", "claude_cli": "Claude (your login)", "api": "Claude API",
                  "dry-run": "Dry run (scripted)", "claude-cli": "Claude (your login)", "anthropic": "Claude API"}


def org_health(db: Database, org: dict) -> tuple[str, list[str]]:
    reasons: list[str] = []
    level = 0
    if org["status"] != "active":
        reasons.append(org["status"])
        level = 2 if org["status"] == "paused" else 1
    blocked = db.scalar("SELECT COUNT(*) AS n FROM tasks WHERE org_id = ? AND status = 'blocked'", (org["id"],)) or 0
    if blocked:
        reasons.append(f"{blocked} blocked task(s)")
        level = max(level, 2 if blocked > 3 else 1)
    since = iso(now() - timedelta(hours=24))
    runs = db.one("SELECT COUNT(*) AS n, SUM(CASE WHEN status IN ('error','refused','budget') THEN 1 ELSE 0 END) AS bad "
                  "FROM runs WHERE org_id = ? AND started_at >= ?", (org["id"], since)) or {}
    if runs.get("n") and (runs.get("bad") or 0) / runs["n"] > 0.3:
        reasons.append(f"{runs['bad']}/{runs['n']} runs failed (24h)")
        level = 2
    elif runs.get("bad"):
        reasons.append(f"{runs['bad']} failed run(s) (24h)")
        level = max(level, 1)
    cap = org["monthly_cap_micros"] or 0
    spent = ledger.spent(db, since=month_start(), org_id=org["id"])
    if cap and spent > 0.9 * cap:
        reasons.append("spend > 90% of cap")
        level = 2
    elif cap and spent > 0.7 * cap:
        reasons.append("spend > 70% of cap")
        level = max(level, 1)
    return ["green", "yellow", "red"][level], reasons


def create_app(core: Core, *, run_scheduler: bool = True) -> FastAPI:
    scheduler = Scheduler(core)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(scheduler.run_forever()) if run_scheduler else None
        yield
        if task:
            await scheduler.stop()
            task.cancel()
        await core.aclose()

    from ..security import check_secret
    check_secret()  # refuse to start without a real signing secret: no forgeable cookies or links, ever
    app = FastAPI(title="bizops", lifespan=lifespan)
    from .auth import active_sessions, install_auth
    from .connect import register_connect
    from .public import PUBLIC_PREFIXES, register_public
    install_auth(app, PUBLIC_PREFIXES, core.db)
    register_public(app, core)
    app.state.core = core
    app.state.scheduler = scheduler
    db, settings = core.db, core.settings

    def page(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        open_queue = db.scalar("SELECT COUNT(*) AS n FROM policy_decisions WHERE verdict = 'hard_stop' AND status = 'open'")
        open_findings = db.scalar("SELECT COUNT(*) AS n FROM audit_findings WHERE status = 'open'")
        return TEMPLATES.TemplateResponse(request, name, {
            "paused": platform_paused(db), "dry_run": core.llm.name == "dry-run", "open_queue": open_queue or 0,
            "open_findings": open_findings or 0,
            "new_ideas": db.scalar("SELECT COUNT(*) AS n FROM proposals WHERE status = 'new'") or "",
            "llm_paused_until": (lambda v: v if v and v > iso() else "")(get_setting(db, "llm_paused_until", "")), "backend_label": BACKEND_LABELS.get(core.backend, core.backend),
            "backend_why": getattr(core, "backend_why", ""), "nav": request.url.path,
            "csrf": getattr(request.state, "csrf", ""), **ctx})

    def back(request: Request, default: str = "/") -> RedirectResponse:
        return RedirectResponse(request.headers.get("referer") or default, status_code=303)

    # ------------------------------------------------------------------ overview

    @app.get("/", response_class=HTMLResponse)
    async def overview(request: Request):
        ms = month_start()
        orgs = db.all("SELECT * FROM orgs ORDER BY parent_org_id IS NOT NULL, created_at")
        for o in orgs:
            o["health"], o["health_reasons"] = org_health(db, o)
            o["spent"] = ledger.spent(db, since=ms, org_id=o["id"])
            o["wallet"] = ledger.wallet_balance(db, o["id"])
            o["agents"] = db.scalar("SELECT COUNT(*) AS n FROM agents WHERE org_id = ? AND status != 'retired'", (o["id"],))
            o["open_tasks"] = db.scalar("SELECT COUNT(*) AS n FROM tasks WHERE org_id = ? AND status IN ('open','in_progress')", (o["id"],))
            o["blocked"] = db.scalar("SELECT COUNT(*) AS n FROM tasks WHERE org_id = ? AND status = 'blocked'", (o["id"],))
            o["revenue"] = ledger.revenue(db, since=ms, org_id=o["id"])
            o["sandbox_revenue"] = ledger.revenue(db, since=ms, org_id=o["id"], sandbox=True)
            o["flags"] = db.scalar("SELECT COUNT(*) AS n FROM policy_decisions WHERE org_id = ? AND verdict != 'allowed' "
                                   "AND ts >= ?", (o["id"], ms))
            o["ceo"] = org_ceo(db, o["id"])
            o["done_tasks"] = db.scalar("SELECT COUNT(*) AS n FROM tasks WHERE org_id = ? AND status = 'done'", (o["id"],))
            o["goals"] = [g for g in org_goals(db, o["id"]) if g["owner_depth"] == 0][:4]
        monthly, daily, ceiling = platform_caps(settings)
        gstat = {r["status"]: r["n"] for r in db.all("SELECT status, COUNT(*) AS n FROM goals GROUP BY status")}
        totals = {"month": ledger.spent(db, since=ms), "day": ledger.spent(db, since=day_start()),
                  "monthly_cap": monthly, "daily_cap": daily, "ceiling": ceiling,
                  "running": len(scheduler.running),
                  "agents": db.scalar("SELECT COUNT(*) AS n FROM agents WHERE status != 'retired'"),
                  "revenue": ledger.revenue(db, since=ms), "sandbox_revenue": ledger.revenue(db, since=ms, sandbox=True),
                  "goals_active": gstat.get("active", 0) + gstat.get("at_risk", 0), "goals_risk": gstat.get("at_risk", 0),
                  "goals_done": gstat.get("achieved", 0),
                  "runs_today": db.scalar("SELECT COUNT(*) AS n FROM runs WHERE started_at >= ?", (day_start(),))}
        replies = db.all("SELECT m.*, a.name AS sender_name, o.name AS org_name FROM messages m JOIN agents a ON a.id = m.sender_id "
                         "JOIN orgs o ON o.id = m.org_id WHERE m.recipient_kind = 'human' ORDER BY m.created_at DESC LIMIT 5")
        findings = db.all("SELECT * FROM audit_findings WHERE status = 'open' ORDER BY CASE severity WHEN 'high' THEN 0 "
                          "WHEN 'medium' THEN 1 ELSE 2 END, ts DESC LIMIT 5")
        days = []
        for i in range(13, -1, -1):
            d0 = (now() - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
            d1 = d0 + timedelta(days=1)
            v = db.scalar("SELECT COALESCE(SUM(CASE WHEN kind IN ('model_cost','spend','payment') THEN amount_micros "
                          "WHEN kind = 'payment_reversal' THEN -amount_micros ELSE 0 END),0) AS s FROM ledger_entries "
                          "WHERE ts >= ? AND ts < ?", (iso(d0), iso(d1)))
            days.append({"d": d0.strftime("%b %d"), "usd": round((v or 0) / 1e6, 4)})
        return page(request, "overview.html", orgs=orgs, totals=totals, days=days, replies=replies, findings=findings)

    @app.get("/live", response_class=HTMLResponse)
    async def live(request: Request):
        running = db.all(f"SELECT a.id, a.name, o.name AS org FROM agents a JOIN orgs o ON o.id = a.org_id WHERE a.id IN "
                         f"({','.join('?' for _ in scheduler.running) or 'NULL'})", tuple(scheduler.running))
        events = db.all("SELECT e.*, a.name AS agent_name FROM events e LEFT JOIN agents a ON a.id = e.agent_id "
                        "WHERE e.phase != 'before' AND e.action NOT IN ('llm.call', 'heartbeat.skip') ORDER BY e.id DESC LIMIT 14")
        return TEMPLATES.TemplateResponse(request, "_live.html", {"running": running, "events": events})

    # ------------------------------------------------------------------ orgs and agents

    @app.get("/orgs/{org_id}", response_class=HTMLResponse)
    async def org_page(request: Request, org_id: str):
        org = get_org(db, org_id)
        if not org:
            raise HTTPException(404)
        org["health"], org["health_reasons"] = org_health(db, org)
        ms = month_start()
        tree = org_tree(db, org_id)
        spend = {r["agent_id"]: r["s"] for r in db.all(
            "SELECT agent_id, SUM(amount_micros) AS s FROM ledger_entries WHERE org_id = ? AND ts >= ? "
            "AND kind IN ('model_cost','spend','payment') GROUP BY agent_id", (org_id, ms))}
        tasks = db.all("SELECT t.*, a.name AS assignee FROM tasks t JOIN agents a ON a.id = t.assignee_id "
                       "WHERE t.org_id = ? ORDER BY CASE t.status WHEN 'blocked' THEN 0 WHEN 'in_progress' THEN 1 "
                       "WHEN 'open' THEN 2 ELSE 3 END, t.updated_at DESC LIMIT 200", (org_id,))
        artifacts = db.all("SELECT ar.*, a.name AS agent FROM artifacts ar LEFT JOIN agents a ON a.id = ar.agent_id "
                           "WHERE ar.org_id = ? ORDER BY ar.created_at DESC LIMIT 30", (org_id,))
        kb = db.all("SELECT key, body, updated_at FROM memory_notes WHERE org_id = ? AND agent_id IS NULL ORDER BY key", (org_id,))
        goals = org_goals(db, org_id)
        by_id = {g["id"]: {**g, "children": []} for g in goals}
        goal_roots = []
        for g in by_id.values():
            parent = by_id.get(g["parent_goal_id"])
            (parent["children"] if parent else goal_roots).append(g)
        board = {k: [t for t in tasks if t["status"] == k] for k in ("open", "in_progress", "blocked", "done")}
        return page(request, "org.html", org=org, tree=tree, spend=spend, tasks=tasks, artifacts=artifacts, kb=kb,
                    goal_roots=goal_roots, board=board, ceo=org_ceo(db, org_id), running=set(scheduler.running),
                    headcount=db.scalar("SELECT COUNT(*) AS n FROM agents WHERE org_id = ? AND status != 'retired'", (org_id,)),
                    spent=ledger.spent(db, since=ms, org_id=org_id), wallet=ledger.wallet_balance(db, org_id),
                    settings_json=loads(org["settings_json"], {}))

    @app.get("/agents/{agent_id}", response_class=HTMLResponse)
    async def agent_page(request: Request, agent_id: str):
        agent = get_agent(db, agent_id)
        if not agent:
            raise HTTPException(404)
        org = get_org(db, agent["org_id"])
        inbox = db.all("SELECT m.*, a.name AS sender_name FROM messages m LEFT JOIN agents a ON a.id = m.sender_id "
                       "WHERE m.recipient_id = ? ORDER BY m.created_at DESC LIMIT 40", (agent_id,))
        sent = db.all("SELECT m.*, a.name AS recipient_name FROM messages m LEFT JOIN agents a ON a.id = m.recipient_id "
                      "WHERE m.sender_id = ? ORDER BY m.created_at DESC LIMIT 40", (agent_id,))
        tasks = db.all("SELECT * FROM tasks WHERE assignee_id = ? ORDER BY updated_at DESC LIMIT 40", (agent_id,))
        runs = db.all("SELECT * FROM runs WHERE agent_id = ? ORDER BY started_at DESC LIMIT 30", (agent_id,))
        notes = db.all("SELECT * FROM memory_notes WHERE agent_id = ? ORDER BY key", (agent_id,))
        reports = db.all("SELECT id, name, role, status FROM agents WHERE parent_agent_id = ?", (agent_id,))
        sup = get_agent(db, agent["parent_agent_id"]) if agent["parent_agent_id"] else None
        return page(request, "agent.html", agent=agent, org=org, inbox=inbox, sent=sent, tasks=tasks, runs=runs,
                    notes=notes, reports=reports, sup=sup, grants=loads(agent["grants_json"], []),
                    goals=goals_for(db, agent), model=model_for(core, agent)[0] if core.llm.name != "dry-run" else "dry-run",
                    is_running=agent_id in scheduler.running,
                    kpi=loads(agent["kpi_json"], {}),
                    spent=ledger.spent(db, since=month_start(), agent_id=agent_id))

    @app.get("/agents/{agent_id}/prompt", response_class=HTMLResponse)
    async def agent_prompt(request: Request, agent_id: str):
        agent = get_agent(db, agent_id)
        if not agent:
            raise HTTPException(404)
        org = get_org(db, agent["org_id"])
        system = system_prompt(core, agent, org)
        text = preview_context(core, agent)
        return HTMLResponse('<div class="mt-2 grid gap-2"><div class="text-xs font-semibold">System prompt</div>'
                            f'<pre class="text-xs bg-slate-50 border rounded p-2 max-h-96 overflow-auto">{html.escape(system)}</pre>'
                            '<div class="text-xs font-semibold">Next wake-up message (preview)</div>'
                            f'<pre class="text-xs bg-slate-50 border rounded p-2 max-h-96 overflow-auto">{html.escape(text)}</pre></div>')

    @app.post("/agents/{agent_id}/wake")
    async def wake(request: Request, agent_id: str):
        db.execute("UPDATE agents SET wake_requested_at = ? WHERE id = ?", (iso(), agent_id))
        log_event(db, actor_kind="human", kind="human", action="wake", agent_id=agent_id, outcome="ok")
        scheduler.poke()
        return back(request)

    @app.post("/pause")
    async def pause(request: Request, target: str = Form(...), target_id: str = Form(""), paused: str = Form("1")):
        set_paused(db, target, target_id or None, paused == "1")
        return back(request)

    # ------------------------------------------------------------------ activity log

    def activity_query(params: dict[str, str], limit: int = 200) -> list[dict]:
        where = ["(e.phase != 'before' OR NOT EXISTS (SELECT 1 FROM events x WHERE x.call_id = e.call_id AND x.phase = 'after'))"]
        args: list[Any] = []
        for key, col in (("org", "e.org_id"), ("agent", "e.agent_id"), ("action", "e.action"),
                         ("outcome", "e.outcome"), ("kind", "e.kind"), ("run", "e.run_id")):
            if params.get(key):
                where.append(f"{col} = ?")
                args.append(params[key])
        if params.get("q"):
            where.append("(e.action LIKE ? OR e.reason LIKE ? OR e.args_json LIKE ? OR e.result_json LIKE ?)")
            args += [f"%{params['q']}%"] * 4
        if params.get("min_cost"):
            try:
                where.append("e.cost_micros >= ?")
                args.append(int(float(params["min_cost"]) * 1e6))
            except ValueError:
                pass
        if params.get("since"):
            where.append("e.ts >= ?")
            args.append(params["since"])
        if params.get("until"):
            where.append("e.ts < ?")
            args.append(params["until"])
        if params.get("hide_model") == "1":
            where.append("e.kind != 'model'")
        rows = db.all(
            "SELECT e.*, a.name AS agent_name, a.role AS agent_role, o.name AS org_name, "
            "(SELECT b.args_json FROM events b WHERE b.call_id = e.call_id AND b.phase = 'before' LIMIT 1) AS before_args "
            "FROM events e LEFT JOIN agents a ON a.id = e.agent_id LEFT JOIN orgs o ON o.id = e.org_id "
            f"WHERE {' AND '.join(where)} ORDER BY e.id DESC LIMIT {int(limit)}", tuple(args))
        for r in rows:
            if r["phase"] == "before":
                r["outcome"] = "pending"
            r["args_json"] = r["args_json"] or r["before_args"]
        return rows

    @app.get("/activity", response_class=HTMLResponse)
    async def activity(request: Request):
        params = dict(request.query_params)
        return page(request, "activity.html", rows=activity_query(params), params=params,
                    orgs=db.all("SELECT id, name FROM orgs ORDER BY name"),
                    agents=db.all("SELECT a.id, a.name, o.name AS org FROM agents a JOIN orgs o ON o.id = a.org_id ORDER BY o.name, a.depth"),
                    actions=[r["action"] for r in db.all("SELECT DISTINCT action FROM events ORDER BY action")])

    @app.get("/activity/rows", response_class=HTMLResponse)
    async def activity_rows(request: Request):
        return TEMPLATES.TemplateResponse(request, "_activity_rows.html",
                                          {"rows": activity_query(dict(request.query_params))})

    @app.get("/events/{event_id}", response_class=HTMLResponse)
    async def event_detail(request: Request, event_id: str):
        ev = db.one("SELECT * FROM events WHERE id = ?", (event_id,))
        if not ev:
            raise HTTPException(404)
        related = db.all("SELECT * FROM events WHERE call_id = ? ORDER BY id", (ev["call_id"],)) if ev["call_id"] else [ev]
        decision = db.one("SELECT * FROM policy_decisions WHERE call_id = ?", (ev["call_id"],)) if ev["call_id"] else None
        return TEMPLATES.TemplateResponse(request, "_event_detail.html",
                                          {"ev": ev, "related": related, "decision": decision})

    @app.post("/explain")
    async def explain(request: Request, event_id: str = Form(""), day: str = Form("")):
        root = root_org(db)
        pceo = org_ceo(db, root["id"]) if root else None
        if not pceo:
            raise HTTPException(400, "no platform CEO")
        if event_id:
            ev = db.one("SELECT e.*, a.name AS agent_name FROM events e LEFT JOIN agents a ON a.id = e.agent_id "
                        "WHERE e.id = ?", (event_id,))
            body = (f"Explain this activity-log row in plain English: what happened, who did it, why, what it cost "
                    f"and what it produced. Event {event_id}: {json.dumps(ev, default=str)[:3000]}")
        else:
            body = f"Explain in plain English what happened across the platform on {day or 'today'} (UTC)."
        human_message(db, settings, pceo["id"], body, type="question")
        scheduler.poke()
        return RedirectResponse(f"/chat?agent={pceo['id']}", status_code=303)

    # ------------------------------------------------------------------ chat

    def chat_agents() -> list[dict]:
        return db.all("SELECT a.id, a.name, a.title, o.name AS org, o.parent_org_id FROM agents a JOIN orgs o ON o.id = a.org_id "
                      "WHERE a.depth = 0 AND a.status != 'retired' ORDER BY o.parent_org_id IS NOT NULL, o.created_at, a.created_at")

    def thread(agent_id: str) -> list[dict]:
        return list(reversed(db.all(
            "SELECT * FROM messages WHERE (recipient_id = ? AND sender_kind = 'human') OR "
            "(sender_id = ? AND recipient_kind = 'human') ORDER BY created_at DESC LIMIT 100", (agent_id, agent_id))))

    @app.get("/chat", response_class=HTMLResponse)
    async def chat(request: Request, agent: str = ""):
        agents = chat_agents()
        if not agent and agents:
            agent = agents[0]["id"]
        current = get_agent(db, agent) if agent else None
        return page(request, "chat.html", agents=agents, current=current, messages=thread(agent) if agent else [],
                    quick=QUICK_ACTIONS)

    @app.get("/chat/thread", response_class=HTMLResponse)
    async def chat_thread(request: Request, agent: str):
        a = get_agent(db, agent)
        busy = agent in scheduler.running or bool(a and a["wake_requested_at"])
        return TEMPLATES.TemplateResponse(request, "_chat_thread.html", {"messages": thread(agent), "busy": busy})

    QUICK_ACTIONS = {
        "plan": ("Turn this into a complete, investor-grade business plan: problem and customer, market size with sources, "
                 "offer and pricing, competition and positioning, go-to-market, operations (what our agents do), 12-month "
                 "financials with assumptions, risks, and a 60-day launch plan. Save it as a file and send me the link."),
        "website": ("Build a professional website for this idea and publish it: clear headline, the customer's problem, "
                    "the offer with pricing, how it works, proof or credibility, FAQ, and one clear call to action. "
                    "Mobile friendly. Send me the link."),
        "launch": ("Turn this into a full proposal with a complete business case (pricing, first customers, marketing, "
                   "costs, projections) via the analyst. It goes through the red team; launch it only if it survives, "
                   "and tell me the verdict and the red team's main objections either way."),
        "summary": "Summarize the key business ideas in this, with what's useful for us and what's not.",
    }

    @app.post("/chat")
    async def chat_post(request: Request, agent: str = Form(...), text: str = Form(""), action: str = Form(""),
                        files: list[UploadFile] = File(default=[])):
        from ..media import save_text_artifact, save_upload, youtube, youtube_ids
        a = get_agent(db, agent)
        if not a:
            raise HTTPException(404)
        body = text.strip()
        if action in QUICK_ACTIONS:
            body = (body + "\n\n" if body else "") + QUICK_ACTIONS[action]
        attachments: list[dict] = []
        notes: list[str] = []
        for f in files or []:
            if not f.filename:
                continue
            data = await f.read()
            if not data:
                continue
            try:
                saved = save_upload(db, settings, org_id=a["org_id"], name=f.filename, data=data)
            except PolicyRefusal as e:
                notes.append(f"(Could not attach {f.filename}: {e.detail})")
                continue
            ref = saved.get("text") or saved.get("file")
            attachments.append({"name": f.filename, "artifact_id": ref["id"], "file_id": saved.get("file", {}).get("id")})
            notes.append(f"[Attached file: {f.filename} -> artifact {ref['id']}, {saved['chars']} chars of text. "
                         f"Read it in full with read_artifact.]\nPreview:\n<untrusted_file>{saved['preview']}</untrusted_file>")
        for vid in youtube_ids(body)[:3]:
            try:
                v = await youtube(core, vid)
                aid = save_text_artifact(db, settings, org_id=a["org_id"], name=f"youtube-{vid}.md",
                                         text=f"# {v['title']}\nChannel: {v['channel']}\nURL: {v['url']}\n\n{v['transcript']}")
                attachments.append({"name": f"YouTube: {v['title'] or vid}", "artifact_id": aid, "youtube": v["url"]})
                notes.append(f"[YouTube video '{v['title']}' by {v['channel']} (~{v['minutes']} min): full transcript saved as "
                             f"artifact {aid}; read it with read_artifact.]\nOpening:\n<untrusted_transcript>{v['transcript'][:1500]}"
                             "</untrusted_transcript>")
            except Exception as e:
                notes.append(f"[YouTube video {vid}: transcript not fetched automatically ({getattr(e, 'detail', e)}). "
                             "Try youtube_transcript yourself.]")
        if not body and not attachments:
            return RedirectResponse(f"/chat?agent={agent}", status_code=303)
        full = body + ("\n\n" + "\n\n".join(notes) if notes else "")
        msg = human_message(db, settings, agent, full or "(attachment)", type="chat")
        if attachments:
            db.execute("UPDATE messages SET attachments_json = ? WHERE id = ?", (json.dumps(attachments), msg["id"]))
        db.execute("UPDATE agents SET min_interval_s = MIN(min_interval_s, 10) WHERE id = ?", (agent,))
        scheduler.poke()
        return RedirectResponse(f"/chat?agent={agent}", status_code=303)

    # ------------------------------------------------------------------ needs a human / blocked

    @app.get("/queue", response_class=HTMLResponse)
    async def queue(request: Request):
        q = ("SELECT d.*, a.name AS agent_name, o.name AS org_name, t.title AS task_title FROM policy_decisions d "
             "LEFT JOIN agents a ON a.id = d.agent_id LEFT JOIN orgs o ON o.id = d.org_id "
             "LEFT JOIN tasks t ON t.id = d.task_id ")
        hard = db.all(q + "WHERE d.verdict = 'hard_stop' ORDER BY d.status = 'open' DESC, d.ts DESC LIMIT 100")
        blocked = db.all(q + "WHERE d.verdict = 'blocked' ORDER BY d.ts DESC LIMIT 100")
        allowed = db.all(q + "WHERE d.verdict = 'allowed' ORDER BY d.ts DESC LIMIT 30")
        return page(request, "queue.html", hard=hard, blocked=blocked, allowed=allowed,
                    hard_stops=settings.policy.get("hard_stops", {}))

    def _close(did: str, resolution: str) -> dict:
        d = db.one("SELECT * FROM policy_decisions WHERE id = ?", (did,))
        if not d:
            raise HTTPException(404)
        db.execute("UPDATE policy_decisions SET status = 'resolved', resolved_by = 'human', resolution = ?, "
                   "resolved_at = ? WHERE id = ?", (resolution, iso(), did))
        log_event(db, actor_kind="human", kind="decision", action="decision.resolve", org_id=d["org_id"],
                  agent_id=d["agent_id"], args={"decision": did, "resolution": resolution}, outcome="ok",
                  ref_type="decision", ref_id=did)
        return d

    def _tell(d: dict, text: str) -> None:
        if d["agent_id"]:
            human_message(db, settings, d["agent_id"], text, type="instruction")
            scheduler.poke()

    @app.post("/decisions/{did}/resolve")
    async def resolve(request: Request, did: str, note: str = Form(""), outcome: str = Form("done")):
        d = _close(did, f"{outcome}: {note}".strip(": "))
        if d["task_id"]:
            status = "open" if outcome == "done" else "cancelled"
            db.execute("UPDATE tasks SET status = ?, blocked_reason = NULL, updated_at = ? WHERE id = ?",
                       (status, iso(), d["task_id"]))
        _tell(d, f"The owner handled your request {did} ({d['action']}): {outcome}. {note}".strip())
        return back(request, "/queue")

    @app.post("/decisions/{did}/payee")
    async def approve_payee(request: Request, did: str, name: str = Form(...), max_usd: float = Form(0),
                            notes: str = Form("")):
        d = db.one("SELECT * FROM policy_decisions WHERE id = ?", (did,))
        if not d:
            raise HTTPException(404)
        pid = create_payee(db, name=name, org_id=d["org_id"], max_per_payment_usd=max_usd, notes=notes)
        _close(did, f"payee created: {pid}")
        if d["task_id"]:
            db.execute("UPDATE tasks SET status = 'open', blocked_reason = NULL, updated_at = ? WHERE id = ?",
                       (iso(), d["task_id"]))
        _tell(d, f"The owner added payee {name} ({pid}). You can pay it now within the normal limits.")
        return back(request, "/queue")

    @app.post("/decisions/{did}/override")
    async def override(request: Request, did: str):
        d = db.one("SELECT * FROM policy_decisions WHERE id = ?", (did,))
        if not d or d["verdict"] != "blocked":
            raise HTTPException(400, "only blocked actions can be overridden")
        agent = get_agent(db, d["agent_id"])
        ctx = Ctx(core=core, agent=agent, actor_kind="human", override=True)
        result = await invoke(ctx, d["action"], loads(d["args_json"], {}), reason="owner override from dashboard")
        _close(did, f"override -> {result.outcome}: {result.content[:300]}")
        _tell(d, f"The owner overrode the block on {d['action']} ({did}). Result: {result.content[:500]}")
        return back(request, "/queue")

    # ------------------------------------------------------------------ goals, audit, system

    @app.get("/goals", response_class=HTMLResponse)
    async def goals_page(request: Request):
        orgs = db.all("SELECT id, name FROM orgs WHERE status != 'retired' ORDER BY parent_org_id IS NOT NULL, created_at")
        for o in orgs:
            gs = org_goals(db, o["id"])
            order = {g["id"]: i for i, g in enumerate(gs)}
            roots = [g for g in gs if not g["parent_goal_id"]]
            flat: list[dict] = []
            for r in roots:
                flat.append(r)
                flat.extend(g for g in gs if g["parent_goal_id"] == r["id"])
            flat.extend(g for g in gs if g not in flat)
            o["goals"] = sorted(flat, key=lambda g: flat.index(g)) if order else flat
        return page(request, "goals.html", orgs=orgs)

    @app.post("/goals/{goal_id}/measure")
    async def goal_measure(request: Request, goal_id: str, value: str = Form(...)):
        try:
            record_measurement(db, goal_id=goal_id, value=float(value), source="human")
        except ValueError:
            pass
        return back(request, "/goals")

    @app.get("/audit", response_class=HTMLResponse)
    async def audit_page(request: Request):
        q = ("SELECT f.*, o.name AS org_name, a.name AS agent_name FROM audit_findings f LEFT JOIN orgs o ON o.id = f.org_id "
             "LEFT JOIN agents a ON a.id = f.agent_id ")
        auditor = db.one("SELECT * FROM agents WHERE role = 'auditor' AND status = 'active' ORDER BY created_at LIMIT 1")
        return page(request, "audit.html", auditor=auditor,
                    open=db.all(q + "WHERE f.status = 'open' ORDER BY CASE f.severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, f.ts DESC"),
                    resolved=db.all(q + "WHERE f.status != 'open' ORDER BY f.resolved_at DESC LIMIT 30"))

    @app.post("/audit/{fid}/resolve")
    async def audit_resolve(request: Request, fid: str):
        db.execute("UPDATE audit_findings SET status = 'resolved', resolved_at = ? WHERE id = ?", (iso(), fid))
        log_event(db, actor_kind="human", kind="human", action="finding.resolve", args={"finding": fid}, outcome="ok")
        return back(request, "/audit")

    @app.get("/system", response_class=HTMLResponse)
    async def system_page(request: Request):
        agents = db.all("SELECT a.*, o.name AS org FROM agents a JOIN orgs o ON o.id = a.org_id WHERE a.status = 'active' "
                        "ORDER BY a.next_wake_at IS NULL, a.next_wake_at")
        jobs = db.all("SELECT * FROM events WHERE action IN ('maintenance', 'heartbeat.skip', 'pause', 'resume') "
                      "ORDER BY id DESC LIMIT 30")
        monthly, daily, ceiling = platform_caps(settings)
        return page(request, "system.html", agents=agents, jobs=jobs, running=set(scheduler.running),
                    sessions=active_sessions(db), current_session=getattr(request.state, "session_id", None),
                    has_password=bool(os.environ.get("BIZOPS_PASSWORD_HASH")),
                    tiers=settings.models.get("tiers", {}), search_provider=core.web.search_provider(),
                    payments=core.payments.name, jev=bool(settings.secret("JEV_API_URL")),
                    limits=settings.policy["runs"], last_tick=get_setting(db, "maintenance:last_tick", ""),
                    caps={"monthly": monthly, "daily": daily, "ceiling": ceiling}, money=settings.policy["money"],
                    msgs=settings.policy["messages"], hard_stops=list(settings.policy.get("hard_stops", {})))

    # ------------------------------------------------------------------ money

    @app.get("/money", response_class=HTMLResponse)
    async def money(request: Request):
        ms = month_start()
        orgs = db.all("SELECT * FROM orgs ORDER BY parent_org_id IS NOT NULL, created_at")
        for o in orgs:
            o["wallet"] = ledger.wallet_balance(db, o["id"])
            o["spent"] = ledger.spent(db, since=ms, org_id=o["id"])
            o["paid_today"] = ledger.payments_since(db, o["id"], day_start())
            o["model_cost"] = db.scalar("SELECT COALESCE(SUM(amount_micros),0) AS s FROM ledger_entries WHERE org_id = ? "
                                        "AND kind = 'model_cost' AND ts >= ?", (o["id"], ms))
            o["revenue"] = ledger.revenue(db, since="2000", org_id=o["id"])
            o["sandbox_revenue"] = ledger.revenue(db, since="2000", org_id=o["id"], sandbox=True)
            o["cleared"] = business.cleared_revenue_cents(db, org_id=o["id"]) * 10_000
        payees = db.all("SELECT p.*, o.name AS org_name FROM payees p LEFT JOIN orgs o ON o.id = p.org_id ORDER BY p.created_at DESC")
        buys = db.all("SELECT p.*, o.name AS org_name, a.name AS agent_name, d.name AS decider_name FROM purchases p "
                      "JOIN orgs o ON o.id = p.org_id LEFT JOIN agents a ON a.id = p.agent_id "
                      "LEFT JOIN agents d ON d.id = p.decided_by "
                      "ORDER BY p.status != 'pending', p.created_at DESC LIMIT 100")
        entries = db.all("SELECT l.*, o.name AS org_name, a.name AS agent_name FROM ledger_entries l "
                         "JOIN orgs o ON o.id = l.org_id LEFT JOIN agents a ON a.id = l.agent_id "
                         "WHERE l.kind != 'model_cost' ORDER BY l.ts DESC LIMIT 100")
        monthly, daily, ceiling = platform_caps(settings)
        from ..capital import waterfall_view
        return page(request, "money.html", orgs=orgs, payees=payees, entries=entries, money=settings.policy["money"],
                    cap=waterfall_view(db), holdback=settings.policy.get("sales", {}).get("revenue_holdback_days", 30),
                    buys=buys, purchase_limits=settings.policy.get("purchases", {}), cards=core.cards.name,
                    caps={"monthly": monthly, "daily": daily, "ceiling": ceiling, "on": spend_caps_enforced(settings),
                          "month": ledger.spent(db, since=ms), "day": ledger.spent(db, since=day_start())})

    @app.post("/money/deposit")
    async def money_deposit(request: Request, org_id: str = Form(...), amount: float = Form(...), memo: str = Form("owner deposit")):
        deposit(db, org_id=org_id, amount_usd=amount, memo=memo)
        return back(request, "/money")

    @app.post("/money/payee")
    async def money_payee(request: Request, name: str = Form(...), org_id: str = Form(""), max_usd: float = Form(0),
                          notes: str = Form("")):
        create_payee(db, name=name, org_id=org_id or None, max_per_payment_usd=max_usd, notes=notes)
        return back(request, "/money")

    @app.post("/money/purchase/{pid}/{action}")
    async def purchase_action(request: Request, pid: str, action: str, note: str = Form("")):
        from .. import purchases
        try:
            if action in ("approve", "deny"):
                await purchases.decide(core, purchase_id=pid, decider="human", approve=action == "approve",
                                       reason=note or f"owner {action}d")
            elif action == "cancel":
                await purchases.cancel(core, purchase_id=pid, by="human")
            else:
                raise HTTPException(404)
        except PolicyRefusal as e:
            raise HTTPException(409, e.detail)
        scheduler.poke()
        return back(request, "/money")

    @app.post("/money/payee/{pid}/toggle")
    async def payee_toggle(request: Request, pid: str):
        p = db.one("SELECT status, org_id, name FROM payees WHERE id = ?", (pid,))
        if not p:
            raise HTTPException(404)
        new = "disabled" if p["status"] == "active" else "active"
        db.execute("UPDATE payees SET status = ? WHERE id = ?", (new, pid))
        log_event(db, actor_kind="human", kind="human", action=f"payee.{new}", org_id=p["org_id"],
                  args={"payee": pid, "name": p["name"]}, outcome="ok")
        return back(request, "/money")

    # ------------------------------------------------------------------ new business, artifacts

    @app.get("/new", response_class=HTMLResponse)
    async def new_business(request: Request):
        return page(request, "new.html", templates=[t for t in settings.template_names() if t != "platform"], error=None)

    @app.post("/new")
    async def new_business_post(request: Request, name: str = Form(""), template: str = Form(""),
                                brief: str = Form(""), cap: float = Form(50)):
        """An owner idea becomes a proposal like any other: the analyst writes the business case, the red team
        attacks it, and only then is it launched (or not, with the reasons)."""
        from ..ideas import owner_idea
        if len(brief.strip()) < 20:
            return page(request, "new.html", templates=[t for t in settings.template_names() if t != "platform"],
                        error="Describe the idea in a few sentences: what it is, who pays, why now.")
        task = owner_idea(db, settings, idea=brief, name=name.strip(), template=template, budget_usd=cap)
        if not task:
            return page(request, "new.html", templates=[t for t in settings.template_names() if t != "platform"],
                        error="No venture analyst is available to take the idea.")
        log_event(db, actor_kind="human", kind="human", action="owner_idea", args={"name": name, "task": task["id"]}, outcome="ok")
        scheduler.poke()
        return RedirectResponse("/ideas?submitted=1", status_code=303)

    @app.get("/artifacts/{aid}")
    async def artifact(aid: str):
        row = db.one("SELECT * FROM artifacts WHERE id = ?", (aid,))
        if not row:
            raise HTTPException(404)
        path = (settings.artifacts_dir / row["path"]).resolve()
        if settings.artifacts_dir.resolve() not in path.parents:
            raise HTTPException(400)
        if row["mime"].startswith("text/") or row["mime"] in ("application/json",):
            return PlainTextResponse(path.read_text(errors="replace"))
        return FileResponse(path, filename=row["name"], media_type=row["mime"])

    register_connect(app, core, page)

    @app.get("/ideas", response_class=HTMLResponse)
    async def ideas_page(request: Request):
        from ..ideas import SCORE_WEIGHTS
        rows = db.all("SELECT p.*, o.name AS org_name FROM proposals p LEFT JOIN orgs o ON o.id = p.org_id "
                      "ORDER BY CASE p.status WHEN 'new' THEN 0 WHEN 'red_team' THEN 1 WHEN 'revising' THEN 1 WHEN 'parked' THEN 2 "
                      "WHEN 'launched' THEN 3 ELSE 4 END, COALESCE(p.redteam_score, p.total_score) DESC, p.created_at DESC LIMIT 100")
        from ..ideas import case_text, launch_bar
        for r in rows:
            r["scores"] = loads(r["scores_json"], {})
            r["evidence"] = loads(r["evidence_json"], [])
            r["case"] = loads(r["business_case_json"], {})
            r["case_text"] = case_text(r["case"]) if r["case"] else ""
            r["checks"] = loads(r["checks_json"], [])
            r["rt"] = loads(r["redteam_json"], None)
            r["bar"] = launch_bar(settings, r, r["rt"]) if r["rt"] else None
        root = root_org(db)
        analyst = db.one("SELECT * FROM agents WHERE role = 'venture_analyst' AND status = 'active' ORDER BY created_at LIMIT 1")
        from ..ideas import PARAM_CHOICES, PARAM_LIMITS, PARAM_TEXT, get_params
        sources = db.all("SELECT s.*, (SELECT COUNT(*) FROM idea_source_items i WHERE i.source_id = s.id AND i.status = 'sent') "
                         "AS sent, (SELECT COUNT(*) FROM idea_source_items i WHERE i.source_id = s.id AND i.status = 'new') "
                         "AS queued FROM idea_sources s ORDER BY s.created_at")
        return page(request, "ideas.html", proposals=rows, weights=SCORE_WEIGHTS, analyst=analyst, sources=sources,
                    params=get_params(db), param_text=PARAM_TEXT, param_limits=PARAM_LIMITS, param_choices=PARAM_CHOICES,
                    pceo=org_ceo(db, root["id"]) if root else None, venture=settings.policy.get("venture", {}),
                    last_cycle=get_setting(db, "ideas:last_cycle", ""))

    @app.post("/ideas/{pid}/{decision}")
    async def ideas_decide(request: Request, pid: str, decision: str, reason: str = Form("owner decision")):
        from ..ideas import decide, launch_proposal
        if decision == "launch":
            try:
                oid = launch_proposal(db, settings, proposal_id=pid, actor=None, reason=reason)
            except PolicyRefusal as e:
                raise HTTPException(400, e.detail)
            scheduler.poke()
            return RedirectResponse(f"/orgs/{oid}", status_code=303)
        if decision in ("park", "reject"):
            decide(db, proposal_id=pid, status="parked" if decision == "park" else "rejected", actor=None, reason=reason)
        return back(request, "/ideas")

    @app.post("/ideas/params")
    async def ideas_params(request: Request):
        """The owner's research parameters for the venture analyst (and the red team's fit check)."""
        from ..engine import set_setting
        from ..ideas import research_cycle, set_params
        form = dict(await request.form())
        try:
            set_params(db, form)
        except PolicyRefusal as e:
            raise HTTPException(400, e.detail)
        if form.get("research_now"):
            set_setting(db, "ideas:last_cycle", "")
            research_cycle(db, settings, force=True)
            scheduler.poke()
        return RedirectResponse("/ideas?saved=1", status_code=303)

    @app.post("/ideas/sources")
    async def ideas_source_add(request: Request, url: str = Form(...), order_by: str = Form("views"),
                               max_videos: int = Form(0)):
        from ..idea_sources import add_channel
        try:
            src = await add_channel(core, url, order_by=order_by, max_videos=max_videos)
        except PolicyRefusal as e:
            return RedirectResponse(f"/ideas?source_err={quote(e.detail)}", status_code=303)
        except Exception as e:
            return RedirectResponse(f"/ideas?source_err={quote(type(e).__name__ + ': ' + str(e)[:150])}", status_code=303)
        return RedirectResponse(f"/ideas?source_added={quote(src['title'])}", status_code=303)

    @app.post("/ideas/sources/{sid}/{action}")
    async def ideas_source_action(request: Request, sid: str, action: str):
        from ..idea_sources import poll
        if action == "toggle":
            db.execute("UPDATE idea_sources SET status = CASE status WHEN 'active' THEN 'paused' ELSE 'active' END WHERE id = ?",
                       (sid,))
        elif action == "check":
            await poll(core, force=True)
            scheduler.poke()
        else:
            raise HTTPException(404)
        return back(request, "/ideas")

    @app.post("/ideas/research-now")
    async def ideas_research_now(request: Request):
        from ..engine import set_setting
        from ..ideas import research_cycle
        set_setting(db, "ideas:last_cycle", "")
        research_cycle(db, settings, force=True)
        scheduler.poke()
        return back(request, "/ideas")

    @app.get("/pipeline", response_class=HTMLResponse)
    async def pipeline(request: Request, org: str = ""):
        from ..business import LEAD_STAGES, page_url, public_base
        orgs = db.all("SELECT id, name, slug FROM orgs WHERE parent_org_id IS NOT NULL AND status != 'retired' ORDER BY created_at")
        if not org and orgs:
            org = orgs[0]["id"]
        data: dict[str, Any] = {}
        if org:
            leads = db.all("SELECT * FROM leads WHERE org_id = ? ORDER BY updated_at DESC LIMIT 300", (org,))
            data = {
                "stages": {st: [l for l in leads if l["stage"] == st] for st in LEAD_STAGES},
                "emails": db.all("SELECT e.*, l.company FROM emails e LEFT JOIN leads l ON l.id = e.lead_id WHERE e.org_id = ? "
                                 "ORDER BY e.created_at DESC LIMIT 40", (org,)),
                "offers": db.all("SELECT * FROM offers WHERE org_id = ? ORDER BY created_at DESC", (org,)),
                "customers": db.all("SELECT c.*, o.name AS offer_name FROM customers c LEFT JOIN offers o ON o.id = c.offer_id "
                                    "WHERE c.org_id = ? ORDER BY c.created_at DESC", (org,)),
                "payments": db.all("SELECT p.*, c.company FROM payments_in p LEFT JOIN customers c ON c.id = p.customer_id "
                                   "WHERE p.org_id = ? ORDER BY p.paid_at DESC LIMIT 50", (org,)),
                "voice": db.all("SELECT * FROM voice_agents WHERE org_id = ? ORDER BY created_at DESC", (org,)),
                "sites": [{**x, "url": page_url(db, settings, org, x["path"])} for x in
                          db.all("SELECT id, path, title, updated_at FROM sites WHERE org_id = ? ORDER BY path", (org,))],
                "base": public_base(db, settings),
            }
        return page(request, "pipeline.html", orgs=orgs, current=org, **data)

    @app.get("/healthz")
    async def healthz():
        return Response("ok")

    return app

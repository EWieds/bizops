"""Background heartbeats: code-only jobs that keep the company moving without anyone asking.

They cost nothing by themselves. They only wake an agent (with a system message) when there is a
reason: a goal review is due, a task stalled, a budget threshold passed, the daily digest is due.
Code-level audit checks record findings directly.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from . import ledger
from .config import Settings
from .db import Database, insert
from .engine import expire_messages, get_agent, get_setting, org_ceo, post_message, root_org, set_setting
from .events import log_event
from .util import day_start, fmt_usd, iso, iso_in, month_start, new_id, now, parse_iso


def _cfg(settings: Settings) -> dict[str, Any]:
    return settings.policy.get("heartbeats", {})


def _due(db: Database, job: str, every_s: float) -> bool:
    last = parse_iso(get_setting(db, f"job:{job}", ""))
    if last and (now() - last).total_seconds() < every_s:
        return False
    set_setting(db, f"job:{job}", iso())
    return True


def _system_msg(db: Database, settings: Settings, agent: dict, body: str, type: str = "instruction") -> None:
    post_message(db, settings, org_id=agent["org_id"], sender_kind="system", sender_id=None, recipient_kind="agent",
                 recipient_id=agent["id"], type=type, body=body)


def goal_reviews(db: Database, settings: Settings) -> int:
    rows = db.all("SELECT g.*, a.status AS owner_status FROM goals g JOIN agents a ON a.id = g.owner_agent_id "
                  "WHERE g.status IN ('active','at_risk') AND g.next_review_at <= ?", (iso(),))
    n = 0
    for g in rows:
        db.execute("UPDATE goals SET next_review_at = ? WHERE id = ?", (iso_in(g["review_every_s"]), g["id"]))
        if g["owner_status"] != "active":
            continue
        owner = get_agent(db, g["owner_agent_id"])
        overdue = parse_iso(g["due_at"]) and parse_iso(g["due_at"]) < now()
        measured = "not measured yet" if g["measured_value"] is None else f"{g['measured_value']} {g['unit']} ({g['measured_source']})"
        body = (f"{'Goal past due' if overdue else 'Goal review due'} [{g['id']}]: {g['title']}\n"
                f"metric: {g['metric']} | target: {g['target']} {g['unit']} | measured: {measured} | "
                f"due {g['due_at'][:10]} | status: {g['status']}\nLast note: {g['progress_note'] or '(none)'}\n"
                + ("Close it out: update_goal to achieved or missed, with the evidence." if overdue else
                   "Review it: what moved since last time (with evidence), is it on track, what's next? "
                   "Then update_goal (status + progress_note) and adjust tasks."))
        _system_msg(db, settings, owner, body)
        n += 1
    return n


def stalled_tasks(db: Database, settings: Settings) -> int:
    cfg = _cfg(settings)
    hours = float(cfg.get("stall_hours", 6))
    cutoff = iso(now() - timedelta(hours=hours))
    rows = db.all("SELECT t.*, a.status AS a_status FROM tasks t JOIN agents a ON a.id = t.assignee_id "
                  "WHERE t.status IN ('open','in_progress') AND t.updated_at < ? "
                  "AND (t.last_nudged_at IS NULL OR t.last_nudged_at < ?)", (cutoff, cutoff))
    n = 0
    for t in rows:
        if t["a_status"] != "active":
            continue
        assignee = get_agent(db, t["assignee_id"])
        db.execute("UPDATE tasks SET last_nudged_at = ?, nudges = nudges + 1 WHERE id = ?", (iso(), t["id"]))
        _system_msg(db, settings, assignee,
                    f"Stalled task [{t['id']}] '{t['title']}' has not moved in {hours:g}h. Finish it, mark it blocked "
                    "with the reason, or hand it back.", type="status")
        if t["nudges"] + 1 >= int(cfg.get("escalate_after_nudges", 2)) and t["created_by"] not in ("human", t["assignee_id"]):
            boss = get_agent(db, t["created_by"])
            if boss and boss["status"] == "active":
                _system_msg(db, settings, boss,
                            f"Escalation: task [{t['id']}] '{t['title']}' assigned to {assignee['name']} has stalled "
                            f"after {t['nudges'] + 1} nudges. Reassign it, unblock it, or cancel it.", type="escalation")
        n += 1
    return n


def budget_alerts(db: Database, settings: Settings) -> int:
    pct = float(_cfg(settings).get("budget_alert_pct", 80)) / 100
    n = 0
    for org in db.all("SELECT * FROM orgs WHERE status IN ('active','trial') AND monthly_cap_micros > 0"):
        spent = ledger.spent(db, since=month_start(), org_id=org["id"])
        if spent < pct * org["monthly_cap_micros"]:
            continue
        key = f"alert:budget:{org['id']}:{day_start()[:10]}"
        if get_setting(db, key):
            continue
        set_setting(db, key, "1")
        msg = (f"Budget alert: {org['name']} has spent {fmt_usd(spent)} of its {fmt_usd(org['monthly_cap_micros'])} "
               "monthly cap. Prioritize the work with measured returns, pause the rest, and tell your supervisor "
               "if the plan needs more budget (with the numbers).")
        targets = db.all("SELECT * FROM agents WHERE org_id = ? AND status = 'active' AND (depth = 0 OR role IN ('cfo','platform_cfo'))",
                         (org["id"],))
        for a in targets:
            _system_msg(db, settings, a, msg, type="status")
        n += 1
    return n


def daily_digest(db: Database, settings: Settings) -> bool:
    hour = int(_cfg(settings).get("digest_hour_utc", 13))
    if now().hour < hour:
        return False
    key = f"digest:{day_start()[:10]}"
    if get_setting(db, key):
        return False
    root = root_org(db)
    ceo = org_ceo(db, root["id"]) if root else None
    if not ceo or ceo["status"] != "active":
        return False
    set_setting(db, key, "1")
    _system_msg(db, settings, ceo,
                "Daily digest is due. Check org_overview, ledger_summary and recent activity, then send the owner "
                "one short message with reply_human: what moved in each business (with numbers), what it cost, open "
                "audit findings, and anything that needs the owner. No fluff.")
    return True


def _finding(db: Database, *, kind: str, severity: str, summary: str, evidence: str, dedupe: str,
             org_id: str | None = None, agent_id: str | None = None, recommendation: str = "") -> bool:
    if db.one("SELECT 1 AS x FROM audit_findings WHERE dedupe_key = ?", (dedupe,)):
        return False
    with db.tx() as conn:
        insert(conn, "audit_findings", {
            "id": new_id("find"), "ts": iso(), "org_id": org_id, "agent_id": agent_id, "source": "system",
            "kind": kind, "severity": severity, "summary": summary, "evidence": evidence,
            "recommendation": recommendation, "status": "open", "dedupe_key": dedupe, "resolved_at": None})
    return True


def audit_checks(db: Database, settings: Settings) -> int:
    n = 0
    day = iso(now() - timedelta(days=1))
    max_hops = int(settings.policy["messages"]["max_hops"])
    for t in db.all("SELECT thread_id, org_id, MAX(hop) AS hops, COUNT(*) AS c FROM messages WHERE created_at >= ? "
                    "GROUP BY thread_id, org_id HAVING MAX(hop) >= ?", (day, max(2, max_hops - 2))):
        n += _finding(db, kind="message_loop", severity="medium", org_id=t["org_id"], dedupe=f"loop:{t['thread_id']}",
                      summary=f"Message thread {t['thread_id']} reached {t['hops']} hops ({t['c']} messages).",
                      evidence=f"thread_id={t['thread_id']}", recommendation="Agents should summarize up instead of replying back and forth.")
    for a in db.all("SELECT agent_id, org_id, SUM(amount_micros) AS s FROM ledger_entries WHERE ts >= ? "
                    "AND kind IN ('model_cost','spend') AND agent_id IS NOT NULL GROUP BY agent_id, org_id", (day,)):
        week = ledger.spent(db, since=iso(now() - timedelta(days=8)), agent_id=a["agent_id"]) - a["s"]
        baseline = max(week / 7, 1_000_000)
        if a["s"] > 3 * baseline:
            n += _finding(db, kind="cost_anomaly", severity="medium", org_id=a["org_id"], agent_id=a["agent_id"],
                          dedupe=f"cost:{a['agent_id']}:{day_start()[:10]}",
                          summary=f"Agent spent {fmt_usd(a['s'])} in 24h, over 3x its baseline ({fmt_usd(int(baseline))}/day).",
                          evidence="ledger_entries last 24h vs prior 7 days", recommendation="Check for loops or wasteful calls.")
    for g in db.all("SELECT * FROM goals WHERE status = 'achieved' AND measured_value IS NULL"):
        n += _finding(db, kind="unsupported_claim", severity="low", org_id=g["org_id"], agent_id=g["owner_agent_id"],
                      dedupe=f"goal-claim:{g['id']}",
                      summary=f"Goal '{g['title']}' marked achieved without a measured value.",
                      evidence=f"goal {g['id']}: metric '{g['metric']}', target {g['target']}, note: {g['progress_note'][:300]}",
                      recommendation="Verify against a connector or have the owner record the measurement.")
    week_ago = iso(now() - timedelta(days=5))
    for a in db.all("SELECT a.id, a.org_id, a.name, (SELECT COUNT(*) FROM runs r WHERE r.agent_id = a.id AND r.started_at >= ?) AS runs, "
                    "(SELECT COUNT(*) FROM tasks t WHERE t.assignee_id = a.id AND t.status = 'done' AND t.updated_at >= ?) AS done "
                    "FROM agents a WHERE a.status = 'active'", (week_ago, week_ago)):
        if a["runs"] >= 8 and a["done"] == 0:
            n += _finding(db, kind="stalled_kpi", severity="low", org_id=a["org_id"], agent_id=a["id"],
                          dedupe=f"stalled:{a['id']}:{day_start()[:10]}",
                          summary=f"{a['name']} ran {a['runs']} times in 5 days and finished no tasks.",
                          evidence="runs vs done tasks, last 5 days", recommendation="Review the role; restructure or retire.")
    return n


def management_reviews(db: Database, settings: Settings) -> int:
    """Wake each manager for a thorough review of their team's work, but only when the team did something."""
    hours = float(_cfg(settings).get("management_review_hours", 12))
    if hours <= 0:
        return 0
    n = 0
    managers = db.all("SELECT DISTINCT m.* FROM agents m JOIN agents r ON r.parent_agent_id = m.id AND r.status = 'active' "
                      "AND r.org_id = m.org_id WHERE m.status = 'active'")
    for m in managers:
        key = f"mgmt_review:{m['id']}"
        last = get_setting(db, key, "")
        if last and (now() - parse_iso(last)).total_seconds() < hours * 3600:
            continue
        since = last or iso(now() - timedelta(hours=hours))
        active = db.scalar("SELECT COUNT(*) AS n FROM events e JOIN agents a ON a.id = e.agent_id WHERE a.parent_agent_id = ? "
                           "AND e.ts >= ? AND e.phase = 'after' AND e.kind = 'tool' AND e.action NOT IN ('yield_run','note_write')",
                           (m["id"], since))
        set_setting(db, key, iso())
        if not active:
            continue
        _system_msg(db, settings, m,
                    "Management review is due. Be thorough: run team_report, open the actual deliverables (read_artifact, "
                    "the published pages, the emails in lead_thread), and judge them against a professional bar. For "
                    "anything below it, send it to the right specialist with request_review and specific feedback (e.g. "
                    "the Head of Growth for copy and pages, the CFO for numbers). Unblock or reassign stuck work, correct "
                    "drift from the goals, recognize what's good, then update your summary note.")
        n += 1
    return n


def run_jobs(db: Database, settings: Settings, *, force: bool = False) -> dict[str, int]:
    """One maintenance pass. Each job throttles itself; `force` runs them all now (tests, CLI)."""
    cfg = _cfg(settings)
    out: dict[str, int] = {}
    if force or _due(db, "expire", 30):
        out["expired"] = expire_messages(db)
    if force or _due(db, "goals", 60):
        out["goal_reviews"] = goal_reviews(db, settings)
    if force or _due(db, "stalled", 300):
        out["stalled"] = stalled_tasks(db, settings)
    if force or _due(db, "budget", 600):
        out["budget_alerts"] = budget_alerts(db, settings)
    if force or _due(db, "digest", 600):
        out["digest"] = int(daily_digest(db, settings))
    if force or _due(db, "audit", float(cfg.get("audit_every_s", 3600))):
        out["findings"] = audit_checks(db, settings)
    from . import business, simworld
    if force or _due(db, "inbound", float(cfg.get("inbound_every_s", 15))):
        out["sim"] = simworld.tick(db, settings)
        out["goals_measured"] = business.measure_goals(db)
    if force or _due(db, "ideas", 600):
        from . import ideas
        out["research_cycle"] = int(ideas.research_cycle(db, settings))
        out["red_team_queued"] = ideas.red_team_backlog(db, settings)
    if force or _due(db, "mgmt", 600):
        out["management_reviews"] = management_reviews(db, settings)
    if force or _due(db, "capital", 3600):
        from .capital import capital_cycle
        out["capital_periods"] = capital_cycle(db, settings)
    if force or _due(db, "followups", 900):
        out["followups"] = business.followups_due(db, settings)
    acted = {k: v for k, v in out.items() if v}
    if acted:
        log_event(db, actor_kind="system", kind="system", action="maintenance", result=acted, outcome="ok")
    set_setting(db, "maintenance:last_tick", iso())
    return out


def heartbeat_needed(db: Database, settings: Settings, agent: dict) -> tuple[bool, str]:
    """Free check before a timer wake: is there anything for this agent to act on?"""
    unread = db.scalar("SELECT COUNT(*) AS n FROM messages WHERE recipient_kind = 'agent' AND recipient_id = ? "
                       "AND status = 'unread'", (agent["id"],))
    if unread:
        return True, "unread messages"
    tasks = db.scalar("SELECT COUNT(*) AS n FROM tasks WHERE assignee_id = ? AND status IN ('open','in_progress')",
                      (agent["id"],))
    if tasks:
        return True, "open tasks"
    has_reports = db.scalar("SELECT COUNT(*) AS n FROM agents WHERE parent_agent_id = ? AND status = 'active'", (agent["id"],))
    from .engine import grants
    if "goals" in grants(agent):
        goals = db.scalar("SELECT COUNT(*) AS n FROM goals WHERE owner_agent_id = ? AND status IN ('active','at_risk')",
                          (agent["id"],))
        if not goals and (agent["depth"] == 0 or has_reports):
            return True, "no goals set"
    if agent["depth"] == 0 or has_reports or agent["role"] in ("auditor", "cfo", "platform_cfo"):
        hours = float(_cfg(settings).get("daily_review_hours", 24))
        last = parse_iso(agent["last_run_at"])
        if not last or (now() - last).total_seconds() > hours * 3600:
            return True, "daily review"
    return False, "nothing to act on"


async def poll_connectors(core) -> dict[str, int]:
    """Pull new replies from the mailbox and new payments from the billing provider."""
    from . import business
    out: dict[str, int] = {}
    for name, fn in (("emails_in", business.poll_email), ("payments_in", business.poll_billing)):
        try:
            out[name] = await fn(core)
        except Exception as e:
            log_event(core.db, actor_kind="system", kind="system", action=f"poll.{name}", result={"error": str(e)[:300]},
                      outcome="error")
    if any(out.values()):
        log_event(core.db, actor_kind="system", kind="system", action="connectors.poll", result=out, outcome="ok")
    return out

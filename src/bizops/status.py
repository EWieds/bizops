"""What each business actually did today, measured from the logs (never from agents' own reports).
Shown on the Overview and given to the platform CEO on every wake, so 'is anything happening?' has a factual answer."""

from __future__ import annotations

from datetime import timedelta

from . import ledger
from .db import Database
from .util import day_start, iso, now

# Events that are bookkeeping, not work.
_QUIET = ("run.start", "run.end", "llm.call", "yield_run", "note_write", "note_read", "maintenance", "heartbeat.skip",
          "model.route", "wake")


def org_today(db: Database, org_id: str) -> dict:
    ds = day_start()
    one = lambda sql, *a: int(db.scalar(sql, a) or 0)  # noqa: E731
    out = {
        "emails_sent": one("SELECT COUNT(*) AS n FROM emails WHERE org_id = ? AND direction = 'out' AND status = 'sent' "
                           "AND created_at >= ?", org_id, ds),
        "replies": one("SELECT COUNT(*) AS n FROM emails WHERE org_id = ? AND direction = 'in' AND created_at >= ?", org_id, ds),
        "leads_added": one("SELECT COUNT(*) AS n FROM leads WHERE org_id = ? AND created_at >= ?", org_id, ds),
        "payments": one("SELECT COUNT(*) AS n FROM payments_in WHERE org_id = ? AND sandbox = 0 AND paid_at >= ?", org_id, ds),
        "pages": one("SELECT COUNT(*) AS n FROM sites WHERE org_id = ? AND updated_at >= ?", org_id, ds),
        "runs": one("SELECT COUNT(*) AS n FROM runs r JOIN agents a ON a.id = r.agent_id WHERE a.org_id = ? AND r.started_at >= ?",
                    org_id, ds),
        "refused": one("SELECT COUNT(*) AS n FROM events WHERE org_id = ? AND ts >= ? AND kind = 'tool' AND outcome = 'refused'",
                       org_id, ds),
        "spent": ledger.spent(db, since=ds, org_id=org_id),
        "waiting_on_owner": one("SELECT COUNT(*) AS n FROM policy_decisions WHERE org_id = ? AND verdict = 'hard_stop' "
                                "AND status = 'open'", org_id),
    }
    marks = ",".join("?" for _ in _QUIET)
    last = db.one(f"SELECT e.ts, e.action, a.name AS agent FROM events e LEFT JOIN agents a ON a.id = e.agent_id "
                  f"WHERE e.org_id = ? AND e.phase != 'before' AND e.outcome = 'ok' AND e.action NOT IN ({marks}) "
                  "ORDER BY e.ts DESC LIMIT 1", (org_id, *_QUIET))
    out["last_action"] = last
    org = db.one("SELECT status FROM orgs WHERE id = ?", (org_id,))
    running = one("SELECT COUNT(*) AS n FROM runs r JOIN agents a ON a.id = r.agent_id WHERE a.org_id = ? "
                  "AND r.status = 'running' AND r.started_at >= ?", org_id, iso(now() - timedelta(minutes=30)))
    if org and org["status"] in ("paused", "retired"):
        out["state"] = org["status"]
    elif running:
        out["state"] = "working now"
    elif last and last["ts"] >= iso(now() - timedelta(hours=2)):
        out["state"] = "active today"
    else:
        out["state"] = "idle"
    return out


def portfolio_text(db: Database) -> str:
    """One line per business for an agent's wake-up context."""
    lines = []
    for o in db.all("SELECT id, name, status FROM orgs WHERE parent_org_id IS NOT NULL AND status != 'retired' "
                    "ORDER BY created_at"):
        t = org_today(db, o["id"])
        last = t["last_action"]
        lines.append(f"- {o['name'][:50]} [{o['id']}] {t['state']}: {t['emails_sent']} emails sent, {t['replies']} replies, "
                     f"{t['leads_added']} new leads, {t['payments']} payments, {t['pages']} pages updated, {t['runs']} agent runs, "
                     f"${t['spent'] / 1e6:.2f} spent, {t['refused']} refused actions"
                     + (f"; last real action {last['action']} by {last['agent'] or 'system'} at {last['ts'][11:16]}Z" if last else "")
                     + (f"; {t['waiting_on_owner']} item(s) waiting on the owner" if t["waiting_on_owner"] else ""))
    return "\n".join(lines) or "- (no businesses yet)"

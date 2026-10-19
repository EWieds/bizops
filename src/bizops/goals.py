"""Goals: measurable, owned, cascaded, reviewed on a cadence.

Agents set goals and report status + progress notes. The measured value is written only by a
connector or the owner, so a goal can't be "achieved" by assertion alone (the auditor checks this).
"""

from __future__ import annotations

import re
from datetime import timedelta

from .config import Settings
from .db import Database, insert
from .engine import PolicyRefusal, get_agent, is_descendant, post_message
from .events import log_event
from .util import iso, iso_in, new_id, now, parse_iso

GOAL_STATUSES = ("active", "at_risk", "achieved", "missed", "dropped")


def _due(due: str | int | float | None) -> str:
    """Accept an ISO date or a number of days from now."""
    if due is None or due == "":
        return iso(now() + timedelta(days=30))
    if isinstance(due, (int, float)) or str(due).isdigit():
        return iso(now() + timedelta(days=float(due)))
    dt = parse_iso(str(due) if "T" in str(due) else f"{due}T23:59:59Z")
    if dt is None:
        raise PolicyRefusal("goal.due", "due must be an ISO date (YYYY-MM-DD) or a number of days")
    return iso(dt)


def set_goal(db: Database, settings: Settings, *, setter: dict | None, owner_id: str, title: str, metric: str,
             target: float | None, unit: str = "", due: str | int | None = None, parent_goal_id: str | None = None,
             review_every_days: float = 3, metric_key: str | None = None) -> dict:
    owner = get_agent(db, owner_id)
    if not owner or owner["status"] == "retired":
        raise PolicyRefusal("goal.owner", f"no active agent {owner_id}")
    if setter is not None and owner_id != setter["id"] and not is_descendant(db, owner_id, setter["id"]):
        raise PolicyRefusal("goal.scope", "you can set goals for yourself or agents below you")
    if not metric.strip():
        raise PolicyRefusal("goal.metric", "a goal needs a metric you can measure")
    active = int(db.scalar("SELECT COUNT(*) AS n FROM goals WHERE owner_agent_id = ? AND status IN ('active','at_risk')",
                           (owner_id,)) or 0)
    if active >= 5:
        raise PolicyRefusal("goal.too_many", "max 5 active goals per agent: focus, or drop one first")
    if parent_goal_id and not db.one("SELECT 1 AS x FROM goals WHERE id = ? AND org_id = ?", (parent_goal_id, owner["org_id"])):
        raise PolicyRefusal("goal.parent", "parent goal not found in this org")
    from .business import METRIC_KEYS
    if metric_key and metric_key not in METRIC_KEYS:
        raise PolicyRefusal("goal.metric_key", f"metric_key must be one of {', '.join(METRIC_KEYS)}")
    dup = setter is not None and duplicate_goal(db, org_id=owner["org_id"], metric=metric, metric_key=metric_key,
                                                parent_goal_id=parent_goal_id)
    if dup:
        raise PolicyRefusal("goal.duplicate", f"goal [{dup['id']}] '{dup['title']}' (owner {dup['owner_agent_id']}) already "
                                              "tracks this metric. Update that goal, drop it first, or make yours its "
                                              "sub-goal with parent_goal_id.")
    review_s = int(max(0.5, float(review_every_days)) * 86400)
    ts = iso()
    row = {
        "id": new_id("goal"), "org_id": owner["org_id"], "owner_agent_id": owner_id, "parent_goal_id": parent_goal_id,
        "title": title.strip(), "metric": metric.strip(), "target": target, "unit": unit or "",
        "measured_value": None, "measured_source": None, "measured_at": None, "due_at": _due(due),
        "review_every_s": review_s, "next_review_at": iso_in(review_s), "status": "active", "progress_note": "",
        "created_by": setter["id"] if setter else "human", "created_at": ts, "updated_at": ts,
        "metric_key": metric_key or None,
    }
    with db.tx() as conn:
        insert(conn, "goals", row)
    if setter is not None and owner_id != setter["id"]:
        post_message(db, settings, org_id=owner["org_id"], sender_kind="system", sender_id=None, recipient_kind="agent",
                     recipient_id=owner_id, type="instruction",
                     body=(f"{setter['name']} gave you a goal [{row['id']}]: {row['title']} "
                           f"(metric: {row['metric']}, target: {row['target']} {row['unit']}, due {row['due_at'][:10]}). "
                           "Plan how you'll hit it: create tasks, and set sub-goals for your reports if you have any."))
    return row


def _metric_id(metric: str, metric_key: str | None) -> str:
    return metric_key or re.sub(r"[^a-z0-9]+", " ", (metric or "").lower()).strip()


def duplicate_goal(db: Database, *, org_id: str, metric: str, metric_key: str | None,
                   parent_goal_id: str | None) -> dict | None:
    """An active goal in the org already tracking the same metric, unless the new goal is part of its cascade
    (a sub-goal under a goal with that metric). Two independent goals for one number is how targets drift."""
    want = _metric_id(metric, metric_key)
    if not want:
        return None
    seen, pid = set(), parent_goal_id
    while pid and pid not in seen:  # a cascade under a goal with this metric is fine
        seen.add(pid)
        p = db.one("SELECT parent_goal_id, metric, metric_key FROM goals WHERE id = ?", (pid,))
        if not p:
            break
        if _metric_id(p["metric"], p["metric_key"]) == want:
            return None
        pid = p["parent_goal_id"]
    for g in db.all("SELECT * FROM goals WHERE org_id = ? AND status IN ('active','at_risk')", (org_id,)):
        if _metric_id(g["metric"], g["metric_key"]) == want:
            return g
    return None


def drop_orphans(db: Database, settings: Settings) -> int:
    """Active goals whose parent was dropped are stale cascades (old targets, old prices): drop them and tell
    their owners to re-plan under the current goals."""
    n = 0
    for g in db.all("SELECT c.*, p.title AS parent_title FROM goals c JOIN goals p ON p.id = c.parent_goal_id "
                    "WHERE c.status IN ('active','at_risk') AND p.status = 'dropped'"):
        db.execute("UPDATE goals SET status = 'dropped', progress_note = ?, updated_at = ? WHERE id = ?",
                   (f"Dropped automatically: its parent goal '{g['parent_title']}' was dropped. {g['progress_note'] or ''}"[:1000],
                    iso(), g["id"]))
        owner = get_agent(db, g["owner_agent_id"])
        if owner and owner["status"] == "active":
            post_message(db, settings, org_id=g["org_id"], sender_kind="system", sender_id=None, recipient_kind="agent",
                         recipient_id=owner["id"], type="instruction",
                         body=(f"Your goal [{g['id']}] '{g['title']}' was dropped because its parent goal was dropped. "
                               "Check your manager's current goals (list_goals scope=org) and work toward those; ask "
                               "your manager if you need a new sub-goal."))
        n += 1
    return n


def update_goal(db: Database, *, actor: dict | None, goal_id: str, status: str | None = None,
                progress_note: str | None = None) -> dict:
    g = db.one("SELECT * FROM goals WHERE id = ?", (goal_id,))
    if not g:
        raise PolicyRefusal("goal.missing", f"no goal {goal_id}")
    if actor is not None and g["owner_agent_id"] != actor["id"] and not is_descendant(db, g["owner_agent_id"], actor["id"]):
        raise PolicyRefusal("goal.scope", "you can only update your goals or your reports' goals")
    if status and status not in GOAL_STATUSES:
        raise PolicyRefusal("goal.status", f"status must be one of {', '.join(GOAL_STATUSES)}")
    if status == "achieved" and g["measured_value"] is None:
        # Not refused (some goals are binary deliverables), but the auditor will look at it.
        progress_note = (progress_note or "") + " [claimed achieved without a measured value]"
    db.execute("UPDATE goals SET status = COALESCE(?, status), progress_note = COALESCE(?, progress_note), "
               "next_review_at = ?, updated_at = ? WHERE id = ?",
               (status, progress_note, iso_in(g["review_every_s"]), iso(), goal_id))
    return db.one("SELECT * FROM goals WHERE id = ?", (goal_id,))


def record_measurement(db: Database, *, goal_id: str, value: float, source: str) -> None:
    """Owner or connector only: the measured value of a goal's metric."""
    if source not in ("human", "connector"):
        raise PermissionError("only the owner or a connector can record measurements")
    g = db.one("SELECT org_id, owner_agent_id FROM goals WHERE id = ?", (goal_id,))
    db.execute("UPDATE goals SET measured_value = ?, measured_source = ?, measured_at = ?, updated_at = ? WHERE id = ?",
               (value, source, iso(), iso(), goal_id))
    log_event(db, actor_kind=source, kind="human" if source == "human" else "system", action="goal.measure",
              org_id=g["org_id"] if g else None, args={"goal": goal_id, "value": value}, outcome="ok",
              ref_type="goal", ref_id=goal_id)


def goals_for(db: Database, agent: dict) -> list[dict]:
    return db.all("SELECT * FROM goals WHERE owner_agent_id = ? AND status IN ('active','at_risk') ORDER BY due_at",
                  (agent["id"],))


def org_goals(db: Database, org_id: str) -> list[dict]:
    return db.all("SELECT g.*, a.name AS owner_name, a.depth AS owner_depth FROM goals g "
                  "JOIN agents a ON a.id = g.owner_agent_id WHERE g.org_id = ? "
                  "ORDER BY CASE g.status WHEN 'at_risk' THEN 0 WHEN 'active' THEN 1 ELSE 2 END, a.depth, g.due_at",
                  (org_id,))

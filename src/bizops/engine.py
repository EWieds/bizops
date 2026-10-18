"""The org engine. The platform and every business are the same thing with different data.

Orgs and agents come from templates (YAML). No role is special in code: an org's CEO is
simply its depth-0 agent, whose supervisor is the platform CEO (the only cross-org edge).
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from . import budget
from .config import Settings
from .db import Database, insert
from .events import log_event
from .util import dumps, iso, iso_in, loads, new_id, usd_to_micros

MESSAGE_TYPES = ("instruction", "question", "answer", "recommendation", "escalation", "status", "chat")


class PolicyRefusal(Exception):
    """Raised when a rule enforced in code refuses an action. `rule` names the rule."""

    def __init__(self, rule: str, detail: str):
        super().__init__(f"{rule}: {detail}")
        self.rule = rule
        self.detail = detail


# ---------------------------------------------------------------- settings / kill switches

def get_setting(db: Database, key: str, default: str = "") -> str:
    row = db.one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def set_setting(db: Database, key: str, value: str) -> None:
    with db.tx() as conn:
        conn.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                     (key, value, iso()))


def platform_paused(db: Database) -> bool:
    return get_setting(db, "platform_paused", "0") == "1"


def set_paused(db: Database, target: str, target_id: str | None, paused: bool, actor: str = "human") -> None:
    """Kill switch: target is 'platform', 'org' or 'agent'."""
    if target == "platform":
        set_setting(db, "platform_paused", "1" if paused else "0")
    elif target in ("org", "agent"):
        table = "orgs" if target == "org" else "agents"
        row = db.one(f"SELECT status FROM {table} WHERE id = ?", (target_id,))
        if not row:
            raise ValueError(f"no such {target}: {target_id}")
        if row["status"] == "retired":
            raise ValueError(f"{target} is retired")
        db.execute(f"UPDATE {table} SET status = ?, updated_at = ? WHERE id = ?",
                   ("paused" if paused else "active", iso(), target_id))
    else:
        raise ValueError(f"bad pause target {target}")
    log_event(db, actor_kind=actor, kind="human" if actor == "human" else "system",
              action="pause" if paused else "resume", org_id=target_id if target == "org" else None,
              agent_id=target_id if target == "agent" else None,
              args={"target": target, "id": target_id}, outcome="ok")


def is_runnable(db: Database, agent: dict) -> tuple[bool, str]:
    if platform_paused(db):
        return False, "platform paused"
    if agent["status"] != "active":
        return False, f"agent {agent['status']}"
    org = db.one("SELECT status FROM orgs WHERE id = ?", (agent["org_id"],))
    if not org or org["status"] not in ("active", "trial"):
        return False, f"org {org['status'] if org else 'missing'}"
    return True, ""


# ---------------------------------------------------------------- orgs and agents

def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "org"


def root_org(db: Database) -> dict | None:
    return db.one("SELECT * FROM orgs WHERE parent_org_id IS NULL ORDER BY created_at LIMIT 1")


def org_ceo(db: Database, org_id: str) -> dict | None:
    return db.one("SELECT * FROM agents WHERE org_id = ? AND depth = 0 AND status != 'retired' "
                  "ORDER BY created_at LIMIT 1", (org_id,))


def get_agent(db: Database, agent_id: str) -> dict | None:
    return db.one("SELECT * FROM agents WHERE id = ?", (agent_id,))


def get_org(db: Database, org_id: str) -> dict | None:
    return db.one("SELECT * FROM orgs WHERE id = ?", (org_id,))


def create_org_from_template(db: Database, settings: Settings, *, template: str, name: str | None = None,
                             brief: str = "", parent_org_id: str | None = None,
                             supervisor_agent_id: str | None = None, created_by: str = "human",
                             monthly_cap_usd: float | None = None) -> str:
    """Insert an org and its starting org chart from a template. Returns the org id."""
    tpl = settings.template(template)
    name = name or tpl.get("name") or template
    cap = usd_to_micros(monthly_cap_usd if monthly_cap_usd is not None else tpl.get("monthly_cap_usd", 0))
    chk = budget.check_allocation(db, settings, new_org_cap=cap)
    if not chk.ok:
        raise PolicyRefusal(chk.rule, chk.detail)
    agent_total = sum(usd_to_micros(a.get("budget_usd", 0)) for a in tpl["agents"])
    if agent_total > cap:
        raise PolicyRefusal("alloc.org", f"template agent budgets exceed org cap ({template})")

    slug = slugify(name)
    if db.one("SELECT 1 AS x FROM orgs WHERE slug = ?", (slug,)):
        slug = f"{slug}-{new_id('x')[-6:]}"
    org_id = new_id("org")
    ts = iso()
    settings_json = dict(tpl.get("settings", {}))
    settings_json["playbook"] = tpl.get("playbook", "")
    min_default = settings.policy["runs"].get("default_min_interval_s", 30)
    with db.tx() as conn:
        insert(conn, "orgs", {
            "id": org_id, "parent_org_id": parent_org_id, "name": name, "slug": slug,
            "template": template, "brief": brief, "status": "active", "monthly_cap_micros": cap,
            "settings_json": dumps(settings_json), "created_at": ts, "updated_at": ts,
        })
        ids: dict[str, str] = {}
        depths: dict[str, int] = {}
        for spec in tpl["agents"]:
            key = spec["key"]
            parent_key = spec.get("parent")
            parent_id = ids[parent_key] if parent_key else supervisor_agent_id
            depth = depths[parent_key] + 1 if parent_key else 0
            aid = new_id("ag")
            ids[key], depths[key] = aid, depth
            wake = spec.get("wake_interval_s")
            insert(conn, "agents", {
                "id": aid, "org_id": org_id, "parent_agent_id": parent_id, "role": spec["role"],
                "name": spec.get("name", spec["role"]), "system_prompt": spec.get("prompt", "").strip(),
                "model_tier": spec.get("tier", "worker"), "model_override": spec.get("model"),
                "grants_json": dumps(spec.get("grants", [])), "kpi_json": dumps(spec.get("kpi", {})),
                "budget_micros": usd_to_micros(spec.get("budget_usd", 0)), "depth": depth,
                "status": "active", "wake_interval_s": wake,
                "min_interval_s": spec.get("min_interval_s", min_default),
                "next_wake_at": iso_in(wake) if wake else None, "wake_requested_at": None,
                "last_run_at": None, "created_by": created_by, "created_at": ts, "updated_at": ts,
            })
        actor = created_by if created_by in ("human", "system") else "agent"
        log_event(conn, actor_kind=actor, kind="spawn",
                  action="create_org", org_id=org_id,
                  agent_id=None if created_by in ("human", "system", "template") else created_by,
                  args={"template": template, "name": name, "cap_micros": cap},
                  result={"agents": list(ids)}, outcome="ok", ref_type="org", ref_id=org_id)
    return org_id


def org_tree(db: Database, org_id: str) -> list[dict]:
    """Agents of an org as a nested tree (list of roots with 'children')."""
    agents = db.all("SELECT * FROM agents WHERE org_id = ? ORDER BY depth, created_at", (org_id,))
    by_id = {a["id"]: {**a, "children": []} for a in agents}
    roots = []
    for a in by_id.values():
        parent = by_id.get(a["parent_agent_id"])
        (parent["children"] if parent else roots).append(a)
    return roots


def is_descendant(db: Database, agent_id: str, ancestor_id: str) -> bool:
    cur = get_agent(db, agent_id)
    seen = set()
    while cur and cur["parent_agent_id"] and cur["id"] not in seen:
        seen.add(cur["id"])
        if cur["parent_agent_id"] == ancestor_id:
            return True
        cur = get_agent(db, cur["parent_agent_id"])
    return False


def team(db: Database, agent: dict) -> dict[str, Any]:
    sup = get_agent(db, agent["parent_agent_id"]) if agent["parent_agent_id"] else None
    reports = db.all("SELECT id, name, role, status FROM agents WHERE parent_agent_id = ? "
                     "AND status != 'retired'", (agent["id"],))
    peers = db.all("SELECT id, name, role FROM agents WHERE org_id = ? AND id != ? AND status = 'active' "
                   "ORDER BY depth, created_at", (agent["org_id"], agent["id"]))
    return {
        "supervisor": {"id": sup["id"], "name": sup["name"], "role": sup["role"]} if sup else None,
        "reports": reports,
        "org_members": peers,
    }


# ---------------------------------------------------------------- messages

def check_message_route(db: Database, sender: dict, recipient: dict) -> None:
    """Peers may only message within their org; across orgs only along a supervisor edge."""
    if recipient["status"] == "retired":
        raise PolicyRefusal("message.recipient", "recipient is retired")
    if recipient["id"] == sender["id"]:
        raise PolicyRefusal("message.self", "cannot message yourself")
    if recipient["org_id"] == sender["org_id"]:
        return
    if recipient["id"] == sender["parent_agent_id"] or recipient["parent_agent_id"] == sender["id"]:
        return
    raise PolicyRefusal("message.scope", "agents may only message within their org or their direct supervisor/reports")


def post_message(db: Database, settings: Settings, *, org_id: str, sender_kind: str, sender_id: str | None,
                 recipient_kind: str, recipient_id: str | None, type: str, body: str,
                 in_reply_to: str | None = None, conn: sqlite3.Connection | None = None) -> dict:
    if type not in MESSAGE_TYPES:
        raise PolicyRefusal("message.type", f"type must be one of {', '.join(MESSAGE_TYPES)}")
    if not body.strip():
        raise PolicyRefusal("message.empty", "empty message")
    max_hops = int(settings.policy["messages"]["max_hops"])
    hop, thread_id = 0, None
    if in_reply_to:
        parent = db.one("SELECT * FROM messages WHERE id = ?", (in_reply_to,))
        if not parent:
            raise PolicyRefusal("message.reply", f"no message {in_reply_to}")
        hop, thread_id = parent["hop"] + 1, parent["thread_id"]
        # A human message resets the counter: humans are never part of a loop.
        if sender_kind == "human":
            hop = 0
    if sender_kind == "agent" and hop > max_hops:
        raise PolicyRefusal("message.max_hops",
                            f"reply chain is {hop} hops deep (max {max_hops}); stop replying and summarize up instead")
    mid = new_id("msg")
    ttl_s = float(settings.policy["messages"]["ttl_hours"]) * 3600
    row = {
        "id": mid, "org_id": org_id, "sender_kind": sender_kind, "sender_id": sender_id,
        "recipient_kind": recipient_kind, "recipient_id": recipient_id, "type": type, "body": body,
        "thread_id": thread_id or mid, "in_reply_to": in_reply_to, "hop": hop,
        "expires_at": iso_in(ttl_s), "status": "unread", "created_at": iso(), "read_at": None,
    }

    def write(c: sqlite3.Connection) -> None:
        insert(c, "messages", row)
        if recipient_kind == "agent":
            c.execute("UPDATE agents SET wake_requested_at = COALESCE(wake_requested_at, ?) WHERE id = ?",
                      (iso(), recipient_id))

    if conn is not None:
        write(conn)
    else:
        with db.tx() as c:
            write(c)
    return row


def human_message(db: Database, settings: Settings, agent_id: str, body: str,
                  type: str = "chat", in_reply_to: str | None = None) -> dict:
    """The owner writes to an agent (CEO chat). Logged like everything else."""
    agent = get_agent(db, agent_id)
    if not agent:
        raise ValueError("no such agent")
    msg = post_message(db, settings, org_id=agent["org_id"], sender_kind="human", sender_id=None,
                       recipient_kind="agent", recipient_id=agent_id, type=type, body=body,
                       in_reply_to=in_reply_to)
    log_event(db, actor_kind="human", kind="message", action="human.message", org_id=agent["org_id"],
              agent_id=agent_id, args={"type": type, "body": body}, outcome="ok",
              ref_type="message", ref_id=msg["id"])
    return msg


def expire_messages(db: Database) -> int:
    with db.tx() as conn:
        cur = conn.execute("UPDATE messages SET status = 'expired' WHERE status = 'unread' AND expires_at < ?",
                           (iso(),))
        return cur.rowcount


# ---------------------------------------------------------------- tasks

def create_task(db: Database, *, creator: dict | None, assignee_id: str, title: str, deliverable: str,
                description: str = "", parent_task_id: str | None = None) -> dict:
    assignee = get_agent(db, assignee_id)
    if not assignee or assignee["status"] == "retired":
        raise PolicyRefusal("task.assignee", f"no active agent {assignee_id}")
    if creator is not None and assignee_id != creator["id"] and not is_descendant(db, assignee_id, creator["id"]):
        raise PolicyRefusal("task.scope", "you can only assign tasks to yourself or agents below you")
    if not deliverable.strip():
        raise PolicyRefusal("task.deliverable", "a task needs a checkable deliverable")
    ts = iso()
    row = {
        "id": new_id("tk"), "org_id": assignee["org_id"], "assignee_id": assignee_id,
        "created_by": creator["id"] if creator else "human", "parent_task_id": parent_task_id,
        "title": title, "description": description, "deliverable": deliverable, "status": "open",
        "attempts": 0, "max_attempts": 3, "result": None, "blocked_reason": None,
        "created_at": ts, "updated_at": ts,
    }
    with db.tx() as conn:
        insert(conn, "tasks", row)
        conn.execute("UPDATE agents SET wake_requested_at = COALESCE(wake_requested_at, ?) WHERE id = ?",
                     (ts, assignee_id))
    return row


TASK_STATUSES = ("open", "in_progress", "done", "blocked", "failed", "cancelled")


def update_task(db: Database, *, actor: dict | None, task_id: str, status: str, result: str | None = None,
                blocked_reason: str | None = None) -> dict:
    task = db.one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if not task:
        raise PolicyRefusal("task.missing", f"no task {task_id}")
    if status not in TASK_STATUSES:
        raise PolicyRefusal("task.status", f"status must be one of {', '.join(TASK_STATUSES)}")
    if actor is not None and task["assignee_id"] != actor["id"] and not is_descendant(db, task["assignee_id"], actor["id"]):
        raise PolicyRefusal("task.scope", "you can only update your own tasks or your reports' tasks")
    attempts = task["attempts"] + (1 if status in ("done", "failed") else 0)
    with db.tx() as conn:
        conn.execute("UPDATE tasks SET status = ?, result = COALESCE(?, result), blocked_reason = ?, attempts = ?, "
                     "updated_at = ? WHERE id = ?",
                     (status, result, blocked_reason if status == "blocked" else None, attempts, iso(), task_id))
        # The delegator hears about finished or stuck work on its next wake.
        if status in ("done", "blocked", "failed") and task["created_by"] not in ("human", task["assignee_id"]):
            conn.execute("UPDATE agents SET wake_requested_at = COALESCE(wake_requested_at, ?) WHERE id = ?",
                         (iso(), task["created_by"]))
    return {**task, "status": status, "result": result or task["result"]}


def kpis(agent: dict) -> dict:
    return loads(agent.get("kpi_json"), {}) or {}


def grants(agent: dict) -> list[str]:
    return loads(agent.get("grants_json"), []) or []

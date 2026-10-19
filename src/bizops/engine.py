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


def agent_row(settings: Settings, *, spec: dict, org_id: str, parent_id: str | None, depth: int,
              created_by: str, ts: str) -> dict:
    """Build an agent row from a role in config/roles.yaml plus per-agent overrides (focus, budget...)."""
    role_key = spec["role"]
    role = settings.role(role_key)
    prompt = role["prompt"].strip()
    if spec.get("focus"):
        prompt += f"\n\nYour specific focus in this org: {spec['focus'].strip()}"
    heartbeat = spec.get("heartbeat_s", role.get("heartbeat_s"))
    min_default = settings.policy["runs"].get("default_min_interval_s", 30)
    return {
        "id": new_id("ag"), "org_id": org_id, "parent_agent_id": parent_id, "role": role_key,
        "name": spec.get("name") or role.get("title", role_key), "title": role.get("title", role_key),
        "system_prompt": prompt, "model_tier": spec.get("tier", role.get("tier", "worker")),
        "model_override": spec.get("model"), "grants_json": dumps(spec.get("grants", role.get("grants", []))),
        "kpi_json": dumps(spec.get("kpi", role.get("kpi", {}))),
        "budget_micros": usd_to_micros(spec.get("budget_usd", 0)), "depth": depth, "status": "active",
        "wake_interval_s": heartbeat, "min_interval_s": spec.get("min_interval_s", min_default),
        "next_wake_at": iso_in(heartbeat) if heartbeat else None, "wake_requested_at": None,
        "last_run_at": None, "created_by": created_by, "created_at": ts, "updated_at": ts,
    }


KICKOFF = """Kickoff. You now run {name}. Read the owner's brief and the playbook, then:
1. Set 2-4 org goals with set_goal (owner: yourself). Each must be measurable, with a metric, a target and
   a due date within 30-60 days. Prefer leading indicators you can actually measure early.
2. Cascade: give each direct report 1-2 goals that ladder up to yours (set_goal with parent_goal_id).
3. Create the first tasks with checkable deliverables (create_task) so work starts today.
4. Hire (spawn_agent) only if a goal needs a skill your team lacks.
Then yield. Goals are reviewed automatically on a cadence; you'll be woken for reviews."""


def create_org_from_template(db: Database, settings: Settings, *, template: str, name: str | None = None,
                             brief: str = "", parent_org_id: str | None = None,
                             supervisor_agent_id: str | None = None, created_by: str = "human",
                             monthly_cap_usd: float | None = None, kickoff: bool = True) -> str:
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
    for spec in tpl["agents"]:
        settings.role(spec["role"])  # fail early on unknown roles

    slug = slugify(name)
    if db.one("SELECT 1 AS x FROM orgs WHERE slug = ?", (slug,)):
        slug = f"{slug}-{new_id('x')[-6:]}"
    org_id = new_id("org")
    ts = iso()
    settings_json = dict(tpl.get("settings", {}))
    settings_json["playbook"] = tpl.get("playbook", "")
    with db.tx() as conn:
        insert(conn, "orgs", {
            "id": org_id, "parent_org_id": parent_org_id, "name": name, "slug": slug,
            "template": template, "brief": brief, "status": "active", "monthly_cap_micros": cap,
            "settings_json": dumps(settings_json), "created_at": ts, "updated_at": ts,
        })
        rows: dict[str, dict] = {}
        for spec in tpl["agents"]:
            parent_key = spec.get("parent")
            if parent_key:
                parent_id, depth = rows[parent_key]["id"], rows[parent_key]["depth"] + 1
            elif spec.get("reports_to_owner"):
                parent_id, depth = None, 0
            else:
                parent_id, depth = supervisor_agent_id, 0
            row = agent_row(settings, spec=spec, org_id=org_id, parent_id=parent_id, depth=depth,
                            created_by=created_by, ts=ts)
            insert(conn, "agents", row)
            rows[spec["key"]] = row
        actor = created_by if created_by in ("human", "system") else "agent"
        log_event(conn, actor_kind=actor, kind="spawn",
                  action="create_org", org_id=org_id,
                  agent_id=None if created_by in ("human", "system", "template") else created_by,
                  args={"template": template, "name": name, "cap_micros": cap},
                  result={"agents": [f"{r['name']} ({r['role']})" for r in rows.values()]}, outcome="ok",
                  ref_type="org", ref_id=org_id)
    ceo = org_ceo(db, org_id)
    if kickoff and ceo:
        post_message(db, settings, org_id=org_id, sender_kind="system", sender_id=None, recipient_kind="agent",
                     recipient_id=ceo["id"], type="instruction", body=KICKOFF.format(name=name))
    return org_id


# ---------------------------------------------------------------- hiring and retiring

def _subtree_size(db: Database, org_id: str) -> int:
    return int(db.scalar("SELECT COUNT(*) AS n FROM agents WHERE org_id = ? AND status != 'retired'", (org_id,)) or 0)


def spawn_agent(db: Database, settings: Settings, *, hirer: dict, role: str, name: str | None = None,
                budget_usd: float = 0, focus: str = "", tier: str | None = None) -> dict:
    """Hire a report under `hirer`. The budget comes out of the hirer's own budget, so children can never
    exceed their parent. Limits: org headcount, tree depth, reports per manager."""
    from . import ledger
    from .util import month_start
    role_def = settings.role(role)
    org = get_org(db, hirer["org_id"])
    limits = loads(org["settings_json"], {}) or {}
    max_agents, max_depth, max_reports = (int(limits.get("max_agents", 12)), int(limits.get("max_depth", 4)),
                                          int(limits.get("max_reports", 6)))
    if _subtree_size(db, org["id"]) >= max_agents:
        raise PolicyRefusal("spawn.max_agents", f"org already has {max_agents} agents; retire or restructure first")
    if hirer["depth"] + 1 >= max_depth:
        raise PolicyRefusal("spawn.max_depth", f"max tree depth is {max_depth}")
    n_reports = int(db.scalar("SELECT COUNT(*) AS n FROM agents WHERE parent_agent_id = ? AND status != 'retired'",
                              (hirer["id"],)) or 0)
    if n_reports >= max_reports:
        raise PolicyRefusal("spawn.max_reports", f"you already have {max_reports} reports")
    amount = usd_to_micros(budget_usd)
    if amount <= 0:
        raise PolicyRefusal("spawn.budget", "give the new agent a budget (budget_usd > 0) from your own budget")
    spent = ledger.spent(db, since=month_start(), agent_id=hirer["id"])
    available = hirer["budget_micros"] - spent
    if amount > available - usd_to_micros(1):
        raise PolicyRefusal("spawn.budget", f"you have ${available / 1e6:.2f} left this month and must keep $1 for yourself")
    hirer_grants = set(grants(hirer))
    role_grants = list(role_def.get("grants", []))
    # A hire never gets money or platform access its manager doesn't have.
    granted = [g for g in role_grants if g not in ("money", "platform_read", "audit") or g in hirer_grants]
    spec = {"role": role, "name": name or role_def.get("title"), "budget_usd": budget_usd, "focus": focus,
            "grants": granted}
    if tier:
        spec["tier"] = tier
    ts = iso()
    row = agent_row(settings, spec=spec, org_id=org["id"], parent_id=hirer["id"], depth=hirer["depth"] + 1,
                    created_by=hirer["id"], ts=ts)
    with db.tx() as conn:
        conn.execute("UPDATE agents SET budget_micros = budget_micros - ?, updated_at = ? WHERE id = ?",
                     (amount, ts, hirer["id"]))
        insert(conn, "agents", row)
    return row


def retire_agent(db: Database, *, actor: dict | None, agent_id: str, reason: str) -> dict:
    """Retire an agent. Its unspent budget returns to its manager, its open tasks and reports move up."""
    target = get_agent(db, agent_id)
    if not target or target["status"] == "retired":
        raise PolicyRefusal("retire.missing", "no such active agent")
    if actor is not None and not is_descendant(db, agent_id, actor["id"]):
        raise PolicyRefusal("retire.scope", "you can only retire agents below you")
    if target["depth"] == 0:
        raise PolicyRefusal("retire.ceo", "an org's CEO can only be retired by the owner")
    parent = target["parent_agent_id"]
    ts = iso()
    with db.tx() as conn:
        conn.execute("UPDATE agents SET status = 'retired', wake_requested_at = NULL, next_wake_at = NULL, "
                     "budget_micros = 0, updated_at = ? WHERE id = ?", (ts, agent_id))
        if parent:
            conn.execute("UPDATE agents SET budget_micros = budget_micros + ?, updated_at = ? WHERE id = ?",
                         (target["budget_micros"], ts, parent))
            conn.execute("UPDATE agents SET parent_agent_id = ?, depth = depth - 1, updated_at = ? "
                         "WHERE parent_agent_id = ?", (parent, ts, agent_id))
            conn.execute("UPDATE tasks SET assignee_id = ?, updated_at = ? WHERE assignee_id = ? "
                         "AND status IN ('open','in_progress','blocked')", (parent, ts, agent_id))
            conn.execute("UPDATE goals SET owner_agent_id = ?, updated_at = ? WHERE owner_agent_id = ? "
                         "AND status IN ('active','at_risk')", (parent, ts, agent_id))
            conn.execute("UPDATE agents SET wake_requested_at = COALESCE(wake_requested_at, ?) WHERE id = ?",
                         (ts, parent))
    return {**target, "status": "retired", "reason": reason}


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
    """Peers may only message within their org; across orgs along a supervisor edge, or between a business's
    CEO and the platform's executives (e.g. the CIO red-teaming an offer)."""
    if recipient["status"] == "retired":
        raise PolicyRefusal("message.recipient", "recipient is retired")
    if recipient["id"] == sender["id"]:
        raise PolicyRefusal("message.self", "cannot message yourself")
    if recipient["org_id"] == sender["org_id"]:
        return
    if recipient["id"] == sender["parent_agent_id"] or recipient["parent_agent_id"] == sender["id"]:
        return
    root = root_org(db)
    if root and {sender["org_id"], recipient["org_id"]} & {root["id"]}:
        other = recipient if sender["org_id"] == root["id"] else sender
        if other["depth"] == 0:
            return
    raise PolicyRefusal("message.scope", "agents may only message within their org, their direct supervisor/reports, "
                                         "or (business CEOs) the platform's executives")


def readable_orgs(db: Database, agent: dict) -> list[str] | None:
    """Orgs whose files and knowledge base an agent may read: its own and the platform's; every org for
    agents with platform oversight (None = all). Writing always stays in the agent's own org."""
    if "platform_read" in grants(agent):
        return None
    root = root_org(db)
    return [agent["org_id"]] + ([root["id"]] if root and root["id"] != agent["org_id"] else [])


def org_filter(db: Database, agent: dict, column: str = "org_id") -> tuple[str, tuple]:
    orgs = readable_orgs(db, agent)
    if orgs is None:
        return "1 = 1", ()
    return f"{column} IN ({','.join('?' * len(orgs))})", tuple(orgs)


AGENT_ALIASES = {"platform_ceo": ("root", "platform_ceo"), "platform ceo": ("root", "platform_ceo"),
                 "cio": ("root", "platform_cfo"), "chief investment officer": ("root", "platform_cfo"),
                 "platform_cfo": ("root", "platform_cfo"), "platform_coo": ("root", "platform_coo"),
                 "auditor": ("root", "auditor"), "red_team": ("root", "red_team"), "red team": ("root", "red_team"),
                 "venture_analyst": ("root", "venture_analyst")}


def resolve_agent_ref(db: Database, sender: dict, ref: str) -> str:
    """Agents often name a colleague by role or name ('ceo', 'Head of Growth') instead of the agent id.
    Resolve that to the id (own org first, then the platform's executives); refuse only if ambiguous."""
    ref = str(ref or "").strip()
    if not ref or db.one("SELECT 1 AS x FROM agents WHERE id = ?", (ref,)):
        return ref
    key = ref.lower().strip().strip("@").replace("-", "_")
    if key in ("supervisor", "boss", "manager", "my supervisor", "parent") and sender.get("parent_agent_id"):
        return sender["parent_agent_id"]
    if key in ("me", "self", "myself"):
        return sender["id"]
    live = "status != 'retired'"
    if key in AGENT_ALIASES or key.replace(" ", "_") in AGENT_ALIASES:
        _, role = AGENT_ALIASES.get(key) or AGENT_ALIASES[key.replace(" ", "_")]
        root = root_org(db)
        row = db.one(f"SELECT id FROM agents WHERE org_id = ? AND role = ? AND {live} ORDER BY created_at LIMIT 1",
                     (root["id"] if root else "", role))
        if row:
            return row["id"]
    for org_id in (sender["org_id"], (root_org(db) or {}).get("id")):
        if not org_id:
            continue
        rows = db.all(f"SELECT id, role, name FROM agents WHERE org_id = ? AND {live} AND "
                      "(lower(role) = ? OR lower(name) = ? OR lower(title) = ?) ORDER BY depth, created_at",
                      (org_id, key.replace(" ", "_"), ref.lower(), ref.lower()))
        if key == "ceo" and not rows:
            rows = db.all(f"SELECT id, role, name FROM agents WHERE org_id = ? AND depth = 0 AND {live}", (org_id,))
        if len(rows) == 1:
            return rows[0]["id"]
        if len(rows) > 1:
            raise PolicyRefusal("agent.ambiguous", f"{ref!r} matches several agents: "
                                + ", ".join(f"{r['name']} ({r['id']})" for r in rows) + "; use the id")
    raise PolicyRefusal("message.recipient", f"no agent {ref!r}; use an agent id from list_team")


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

"""One-time owner setup (Stripe, the mailbox) is ONE item in the owner's queue, never one per business.

Agents in every business hit the same missing connection and used to file it again and again, under
whatever category came to mind ("identity_account", "external_transfer"...). Now:
  * any hard stop whose text is about a platform connection is folded into the single setup item for that
    connection (created once), the agent is told it's handled, and its task is NOT blocked;
  * if the connection already works, the agent is told so instead;
  * as soon as a connection works (Connect page, or secrets set on the server), the setup items close and
    every agent who asked is told to go ahead.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .db import Database
from .engine import get_agent, post_message, root_org
from .events import log_event
from .util import iso, new_id

TOPICS = {
    "billing": (re.compile(r"\bstripe\b|payment link|checkout link|create_offer|billing account|\bpayments? (provider|account)",
                           re.I), "the umbrella Stripe account (Connect page)"),
    "email": (re.compile(r"\bsmtp\b|\bimap\b|mailbox|email account|sending (domain|address)|postal address|connect (the )?email",
                         re.I), "the mailbox agents send from, with the postal address (Connect page)"),
}
FOLDABLE = {"hard_stop.identity_account", "hard_stop.external_transfer", "hard_stop.connect_billing",
            "hard_stop.connect_email", "hard_stop.legal_commitment"}


def topics_in(text: str) -> list[str]:
    return [t for t, (rx, _) in TOPICS.items() if rx.search(text or "")]


def is_connected(core: Any, topic: str) -> bool:
    c = core.connectors
    return bool({"billing": c.billing.live, "email": c.email.live}[topic])


def setup_item(db: Database, topic: str) -> str:
    """The single open queue item for a connection; created once."""
    rule = f"hard_stop.connect_{topic}"
    row = db.one("SELECT id FROM policy_decisions WHERE rule = ? AND status = 'open' ORDER BY ts LIMIT 1", (rule,))
    if row:
        return row["id"]
    did = new_id("pd")
    root = root_org(db)
    db.execute("INSERT INTO policy_decisions (id, ts, org_id, action, args_json, verdict, rule, status) "
               "VALUES (?, ?, ?, 'owner_setup', ?, 'hard_stop', ?, 'open')",
               (did, iso(), root["id"] if root else None,
                json.dumps({"summary": f"One-time setup for every business: connect {TOPICS[topic][1]}."}), rule))
    return did


def fold(core: Any, rule: str, args: dict) -> str | None:
    """If this hard stop is really a platform-connection request, answer it without a new queue item.
    Returns the message for the agent, or None to file it normally."""
    if rule not in FOLDABLE:
        return None
    topics = topics_in(json.dumps(args, ensure_ascii=False))
    if not topics:
        return None
    parts = []
    for t in topics:
        if is_connected(core, t):
            parts.append(f"{TOPICS[t][1].split(' (')[0]} is ALREADY connected: use the tools that need it now")
        else:
            parts.append(f"{TOPICS[t][1].split(' (')[0]} is already in the owner's queue as one setup item for all "
                         f"businesses ({setup_item(core.db, t)}); you'll get a message when it's connected")
    return ("NOT FILED: " + "; ".join(parts) + ". Never file platform setup again. Keep working: prepare everything "
            "else so it can go live the moment it's connected. If you need something else from the owner, file it "
            "separately without mentioning these.")


def business_address(core: Any, org_id: str) -> str | None:
    """A business's own email address: the mailbox with +<business> (mailbox+slug@domain). Mail to it reaches
    that business. None until the owner connects a mailbox."""
    base = core.settings.secret("EMAIL_FROM") or core.settings.secret("SMTP_USER") or ""
    if "@" not in base or not core.connectors.email.live:
        return None
    org = core.db.one("SELECT slug, parent_org_id FROM orgs WHERE id = ?", (org_id,))
    local, domain = base.rsplit("@", 1)
    if not org or org["parent_org_id"] is None:
        return base
    return f"{local.split('+')[0]}+{org['slug']}@{domain}"


def status_lines(core: Any, org_id: str) -> list[str]:
    """What's connected, for the agents' wake-up context, so nobody asks the owner for it."""
    out = []
    for t, (_, label) in TOPICS.items():
        name = label.split(" (")[0]
        out.append(f"- {name}: " + ("connected" if is_connected(core, t) else
                                    "NOT connected yet; the owner already has it as one setup item. Never file it. "
                                    "Prepare everything that needs it; you'll be told when it's connected"))
    addr = business_address(core, org_id)
    if addr:
        out.append(f"- your business's email address: {addr} (put it on your pages; mail to it reaches you)")
    out.append(f"- virtual cards for approved purchases: {core.cards.name}; "
               f"browser: {'available' if core.browser.installed() else 'not installed'}")
    return out


async def resolve_connected(core: Any) -> int:
    """Close setup items whose connection now works and tell every agent who was waiting on it."""
    db, n = core.db, 0
    for topic in TOPICS:
        if not is_connected(core, topic):
            continue
        rows = db.all("SELECT * FROM policy_decisions WHERE status = 'open' AND rule = ?", (f"hard_stop.connect_{topic}",))
        if not rows:
            continue
        waiting = {r["agent_id"] for r in rows if r["agent_id"]}
        waiting |= {r["agent_id"] for r in db.all("SELECT DISTINCT agent_id FROM events WHERE kind = 'decision' AND "
                                                   "result_json LIKE ? AND agent_id IS NOT NULL", (f"%connect_{topic}%",))}
        for r in rows:
            db.execute("UPDATE policy_decisions SET status = 'resolved', resolved_by = 'system', resolution = ?, "
                       "resolved_at = ? WHERE id = ?", ("connected", iso(), r["id"]))
            if r["task_id"]:
                db.execute("UPDATE tasks SET status = 'open', blocked_reason = NULL, updated_at = ? "
                           "WHERE id = ? AND status = 'blocked'", (iso(), r["task_id"]))
        label = TOPICS[topic][1].split(" (")[0]
        for aid in waiting:
            a = get_agent(db, aid)
            if a and a["status"] == "active":
                post_message(db, core.settings, org_id=a["org_id"], sender_kind="system", sender_id=None,
                             recipient_kind="agent", recipient_id=aid, type="instruction",
                             body=f"{label} is now connected. Redo what was waiting on it (offers, payment links, "
                                  "emails) now.")
        log_event(db, actor_kind="system", kind="system", action=f"setup.{topic}.connected",
                  result={"closed": len(rows), "told": len(waiting)}, outcome="ok")
        n += len(rows)
    return n

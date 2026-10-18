"""The ideas pipeline: the venture analyst researches on its own every few days and submits scored
proposals; the platform CEO (or the owner) approves, launches or rejects them. Launching turns a
proposal into a business on trial, inside the venture budget."""

from __future__ import annotations

import json
import re
from typing import Any

from .config import Settings
from .db import Database, insert
from .engine import PolicyRefusal, create_org_from_template, get_setting, org_ceo, post_message, root_org, set_setting
from .events import log_event
from .util import iso, month_start, new_id, now, parse_iso

# Each criterion is scored 1-5 by the analyst; "risk"-type criteria are scored so that 5 is best (lowest risk).
SCORE_WEIGHTS = {
    "demand": 0.22,            # evidence people pay to solve this
    "time_to_revenue": 0.18,   # 5 = first payment within 2-3 weeks
    "automation_fit": 0.16,    # 5 = agents can deliver it end to end
    "cost_to_test": 0.12,      # 5 = under $50 to test
    "competition": 0.10,       # 5 = room for a small, specific player
    "legal_risk": 0.12,        # 5 = low legal/regulatory risk
    "owner_steps": 0.10,       # 5 = few or no owner-only steps
}


def score_total(scores: dict[str, Any]) -> float:
    total = 0.0
    for k, w in SCORE_WEIGHTS.items():
        try:
            v = min(5.0, max(1.0, float(scores.get(k, 1))))
        except (TypeError, ValueError):
            v = 1.0
        total += w * v
    return round(total, 2)


def submit_proposal(db: Database, settings: Settings, *, author: dict | None, fields: dict[str, Any]) -> dict:
    for k in ("title", "summary", "thesis", "target_customer", "offer", "plan", "kill_criteria"):
        if not str(fields.get(k, "")).strip():
            raise PolicyRefusal("proposal.incomplete", f"a proposal needs {k}")
    evidence = fields.get("evidence") or []
    if not isinstance(evidence, list) or not any(isinstance(e, dict) and e.get("source_url") for e in evidence):
        raise PolicyRefusal("proposal.evidence", "include evidence with source URLs")
    norm = re.sub(r"[^a-z0-9]+", " ", str(fields["title"]).lower()).strip()
    for other in db.all("SELECT id, title, status FROM proposals WHERE status != 'rejected'"):
        if re.sub(r"[^a-z0-9]+", " ", other["title"].lower()).strip() == norm:
            raise PolicyRefusal("proposal.duplicate", f"already proposed as {other['id']} ({other['status']}); "
                                                      "propose something different or improve that one")
    templates = settings.policy.get("venture", {}).get("templates", ["generic"])
    template = fields.get("template") or "generic"
    if template not in templates:
        template = "generic"
    scores = fields.get("scores") or {}
    ts = iso()
    row = {"id": new_id("prop"), "title": fields["title"].strip()[:140], "summary": fields["summary"].strip(),
           "thesis": fields["thesis"].strip(), "target_customer": fields["target_customer"].strip(),
           "offer": fields["offer"].strip(), "evidence_json": json.dumps(evidence), "plan": fields["plan"].strip(),
           "starting_budget_usd": float(fields.get("starting_budget_usd") or 50), "kill_criteria": fields["kill_criteria"].strip(),
           "template": template, "scores_json": json.dumps(scores), "total_score": score_total(scores),
           "source": fields.get("source") or "research", "source_ref": fields.get("source_ref"), "status": "new",
           "org_id": None, "artifact_id": fields.get("artifact_id"), "created_by": author["id"] if author else "human",
           "decided_by": None, "decision_reason": None, "created_at": ts, "updated_at": ts}
    with db.tx() as conn:
        insert(conn, "proposals", row)
    root = root_org(db)
    ceo = org_ceo(db, root["id"]) if root else None
    if ceo:
        auto = settings.policy.get("venture", {}).get("auto_launch", True)
        min_score = float(settings.policy.get("venture", {}).get("auto_launch_min_score", 3.6))
        post_message(db, settings, org_id=root["id"], sender_kind="system", sender_id=None, recipient_kind="agent",
                     recipient_id=ceo["id"], type="instruction",
                     body=(f"New proposal [{row['id']}] '{row['title']}' scored {row['total_score']}/5. "
                           + (f"Standing authorization: if it is strong (>= {min_score}) and within the venture budget, "
                              "launch it with decide_proposal(approve); otherwise park or reject it with a reason, or ask "
                              "the analyst for more evidence. Tell the owner what you decided."
                              if auto else "The owner decides launches: review it and recommend a decision to the owner.")))
    return row


def proposal_brief(p: dict) -> str:
    ev = "\n".join(f"- {e.get('claim', '')} ({e.get('source_url', '')})" for e in json.loads(p["evidence_json"] or "[]"))
    return (f"{p['summary']}\n\nThesis: {p['thesis']}\nTarget customer: {p['target_customer']}\nOffer: {p['offer']}\n"
            f"Evidence:\n{ev}\n\n60-day plan:\n{p['plan']}\n\nStarting budget: ${p['starting_budget_usd']:.0f}/month\n"
            f"Kill criteria: {p['kill_criteria']}")


def launch_proposal(db: Database, settings: Settings, *, proposal_id: str, actor: dict | None, reason: str = "") -> str:
    """Turn a proposal into a business on trial. Agents are bound by the venture budget; the owner isn't."""
    p = db.one("SELECT * FROM proposals WHERE id = ?", (proposal_id,))
    if not p:
        raise PolicyRefusal("proposal.missing", "no such proposal")
    if p["status"] == "launched":
        raise PolicyRefusal("proposal.launched", f"already launched as {p['org_id']}")
    v = settings.policy.get("venture", {})
    if actor is not None:
        made = int(db.scalar("SELECT COUNT(*) AS n FROM proposals WHERE status = 'launched' AND updated_at >= ? "
                             "AND decided_by != 'human'", (month_start(),)) or 0)
        if made >= int(v.get("max_new_orgs_per_month", 2)):
            raise PolicyRefusal("venture.monthly", "venture budget: max new businesses this month reached")
        if int(db.scalar("SELECT COUNT(*) AS n FROM orgs WHERE status = 'trial'") or 0) >= int(v.get("max_on_trial", 3)):
            raise PolicyRefusal("venture.trial", "too many businesses on trial; review or close one first")
    root = root_org(db)
    pceo = org_ceo(db, root["id"])
    cap = min(float(p["starting_budget_usd"]), float(v.get("starting_cap_usd", 50))) if actor is not None else float(p["starting_budget_usd"])
    oid = create_org_from_template(db, settings, template=p["template"], name=p["title"][:60], brief=proposal_brief(p),
                                   parent_org_id=root["id"], supervisor_agent_id=pceo["id"],
                                   created_by=actor["id"] if actor else "human", monthly_cap_usd=cap)
    db.execute("UPDATE orgs SET status = 'trial' WHERE id = ?", (oid,))
    db.execute("UPDATE proposals SET status = 'launched', org_id = ?, decided_by = ?, decision_reason = ?, updated_at = ? "
               "WHERE id = ?", (oid, actor["id"] if actor else "human", reason, iso(), proposal_id))
    log_event(db, actor_kind="agent" if actor else "human", kind="decision", action="proposal.launch",
              org_id=oid, agent_id=actor["id"] if actor else None, args={"proposal": proposal_id, "reason": reason},
              outcome="ok", ref_type="org", ref_id=oid)
    return oid


def decide(db: Database, *, proposal_id: str, status: str, actor: dict | None, reason: str) -> None:
    db.execute("UPDATE proposals SET status = ?, decided_by = ?, decision_reason = ?, updated_at = ? WHERE id = ?",
               (status, actor["id"] if actor else "human", reason, iso(), proposal_id))


def research_cycle(db: Database, settings: Settings) -> bool:
    """Every N days, give the venture analyst a research task (code only; the analyst does the work)."""
    v = settings.policy.get("venture", {})
    every = float(v.get("research_every_days", 3)) * 86400
    if every <= 0:
        return False
    last = parse_iso(get_setting(db, "ideas:last_cycle", ""))
    if last and (now() - last).total_seconds() < every:
        return False
    root = root_org(db)
    if not root:
        return False
    analyst = db.one("SELECT * FROM agents WHERE org_id = ? AND role = 'venture_analyst' AND status = 'active' "
                     "ORDER BY created_at LIMIT 1", (root["id"],))
    if not analyst:
        return False
    if db.one("SELECT 1 AS x FROM tasks WHERE assignee_id = ? AND status IN ('open','in_progress') AND title LIKE 'Research cycle%'",
              (analyst["id"],)):
        return False
    set_setting(db, "ideas:last_cycle", iso())
    from .engine import create_task
    n = int(v.get("proposals_per_cycle", 3))
    create_task(db, creator=None, assignee_id=analyst["id"], title=f"Research cycle: propose {n} new businesses",
                deliverable=f"{n} complete proposals submitted with submit_proposal (scored, with sourced evidence)",
                description=("Scan markets, trends and what's working in our portfolio. Prefer B2B offers our agents can "
                             "build, sell by email and deliver themselves, with a first payment possible in 2-3 weeks and "
                             "low legal risk. Skip ideas we already proposed (list_proposals). Score honestly."))
    return True

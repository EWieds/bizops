"""Scripted, deterministic, role-aware fake model. Same interface as the real client.

It reads the <context_json> block the runtime puts in every wake-up message and plays out a
plausible plan through real tool calls, so the whole engine (gateway, policy, budget,
approver, ledger, scheduler, dashboard) runs for $0.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from ..config import Settings
from ..util import clip, usd_to_micros
from .base import LLMResponse, SearchResult

Call = tuple[str, dict]
Turn = list[Call] | Callable[[list[dict]], list[Call]]


def _ctx(messages: list[dict]) -> dict:
    first = messages[0]["content"] if messages else ""
    if isinstance(first, list):
        first = " ".join(b.get("text", "") for b in first if isinstance(b, dict))
    m = re.search(r"<context_json>\s*(\{.*\})\s*</context_json>", first, re.S)
    return json.loads(m.group(1)) if m else {}


def _last_results(messages: list[dict]) -> list[str]:
    if not messages or messages[-1]["role"] != "user" or not isinstance(messages[-1]["content"], list):
        return []
    return [str(b.get("content", "")) for b in messages[-1]["content"] if b.get("type") == "tool_result"]


def _pick_report(reports: list[dict], text: str) -> dict | None:
    if not reports:
        return None
    t = text.lower()
    prefs = [("research", "researcher"), ("market", "marketer"), ("copy", "writer"), ("write", "writer"),
             ("plan", "coo"), ("budget", "cfo"), ("cost", "cfo")]
    for word, role in prefs:
        if word in t:
            for r in reports:
                if r["role"] == role:
                    return r
    for role in ("coo", "researcher", "marketer", "writer"):
        for r in reports:
            if r["role"] == role:
                return r
    return reports[0]


def _result_id(hist: list[dict], prefix: str) -> str | None:
    for r in _last_results(hist):
        m = re.search(rf"\b({prefix}_[0-9a-f]+)\b", r)
        if m:
            return m.group(1)
    return None


def _system(m: dict, agent: dict, grants: list, reports: list, first: list, later: list, did: list) -> None:
    """Scripted responses to the platform's automatic messages (kickoff, reviews, nudges, digest)."""
    body = m["body"]
    if body.startswith("Kickoff") and "goals" in grants:
        first.append(("set_goal", {"owner": agent["id"], "title": "Prove someone will pay", "metric": "paying customers",
                                   "target": 1, "unit": "customers", "due": "45", "reason": "the first thing to prove"}))
        first.append(("set_goal", {"owner": agent["id"], "title": "Build a lead pipeline", "metric": "qualified leads",
                                   "target": 20, "unit": "leads", "due": "30", "reason": "leading indicator for revenue"}))

        def cascade(hist: list[dict]) -> list[Call]:
            gid = _result_id(hist, "goal")
            calls: list[Call] = []
            for r in reports[:2]:
                calls.append(("set_goal", {"owner": r["id"], "title": f"{r['role'].upper()} contribution to the lead pipeline",
                                           "metric": "deliverables shipped", "target": 3, "unit": "deliverables",
                                           "due": "21", "parent_goal_id": gid, "reason": "cascade the org goal"}))
            return calls or [("note_write", {"key": "summary", "body": "Goals set.", "reason": "remember"})]
        later.append(cascade)
        did.append("set goals at kickoff")
    elif body.startswith(("Goal review due", "Goal past due")):
        gid = re.search(r"\[(goal_[0-9a-f]+)\]", body)
        if gid:
            past = body.startswith("Goal past due")
            first.append(("update_goal", {"goal_id": gid.group(1), "status": "missed" if past else "active",
                                          "progress_note": "Dry-run review: work in progress, nothing measured yet.",
                                          "reason": "scheduled goal review"}))
            did.append("reviewed a goal")
    elif "gave you a goal" in body:
        gid = re.search(r"\[(goal_[0-9a-f]+)\]", body)
        if reports:
            first.append(("create_task", {"assignee": reports[0]["id"], "title": "Work toward my new goal",
                                          "deliverable": "A short plan saved with write_artifact",
                                          "description": body[:400], "reason": "turn the goal into work"}))
        else:
            first.append(("note_write", {"key": "goal-plan", "body": f"Plan for {gid.group(1) if gid else 'goal'}: ship 3 deliverables.",
                                         "reason": "plan toward the goal"}))
        did.append("planned toward a new goal")
    elif body.startswith("Daily digest") and "platform_read" in grants:
        first.append(("org_overview", {"reason": "gather numbers for the digest"}))
        later.append(lambda hist: [("reply_human", {"text": "Daily digest (dry-run):\n" + clip(" ".join(_last_results(hist)), 700),
                                                    "reason": "daily digest for the owner"})])
        did.append("sent the daily digest")
    elif body.startswith("Stalled task"):
        tid = re.search(r"\[(tk_[0-9a-f]+)\]", body)
        if tid:
            first.append(("update_task", {"task_id": tid.group(1), "status": "blocked",
                                          "blocked_reason": "dry-run: can't progress without real work",
                                          "reason": "be honest about the stall"}))
    else:
        first.append(("note_write", {"key": "platform-notices", "body": clip(body, 400), "reason": "keep a log"}))


# ---------------------------------------------------------------- selling flows (each a list of turns)

INTERESTED = re.compile(r"\b(cost|price|pricing|interesting|try it|sounds)\b", re.I)
SIGNED = re.compile(r"signed up", re.I)
TOO_PRICEY = re.compile(r"more than we can|budget would be", re.I)
LATER = re.compile(r"not right now|next quarter", re.I)
BRIEF = ("Thesis: local service businesses (dental, HVAC, plumbing) miss 20-40% of inbound calls, and each missed "
         "call can be a lost $200-$2,000 job. An AI phone receptionist that answers every call, handles FAQs and takes "
         "booking requests is now cheap to deliver by white-labeling a voice platform. Customer: owner-operated local "
         "service businesses in Columbus, OH. Offer: AI receptionist at $149/month. Evidence: competitor pricing "
         "$99-$399/month; strong search interest in 'AI receptionist'. 60-day plan: demo + site in week 1, 100 "
         "personalized emails with live demos by week 3, first 3 paying customers by day 45. Kill criteria: fewer "
         "than 2 paying customers or under 5% reply rate by day 60.")


def _ids(text: str, prefix: str) -> list[str]:
    return list(dict.fromkeys(re.findall(rf"\b({prefix}_[0-9a-f]+)\b", text)))


def _all_results(hist: list[dict]) -> str:
    return " ".join(_last_results(hist))


def selling_flows(ctx: dict) -> tuple[list[list[Turn]], set[str], set[str]]:
    """Returns (flows, handled message ids, handled task ids)."""
    agent = ctx.get("agent", {})
    g = set(agent.get("grants", []))
    reports = (ctx.get("team") or {}).get("reports") or []
    sup = (ctx.get("team") or {}).get("supervisor")
    flows: list[list[Turn]] = []
    done_msgs: set[str] = set()
    done_tasks: set[str] = set()
    for m in ctx.get("inbox", []):
        body = m["body"]
        # Platform: the owner asks for a new business -> analyst researches and proposes.
        if "venture" in g and m["from_kind"] == "human" and re.search(r"\b(launch|new business|find a)\b", body, re.I):
            analyst = next((r for r in reports if r["role"] == "venture_analyst"), None)
            if analyst:
                flows.append([[("create_task", {"assignee": analyst["id"], "title": "Research and propose a new business we can run",
                                                "deliverable": "A proposal artifact: thesis, evidence with sources, customer, offer, 60-day plan, kill criteria",
                                                "reason": "the venture analyst finds and scores opportunities"}),
                               ("reply_human", {"in_reply_to": m["id"], "text": "On it. Our venture analyst is researching "
                                                "opportunities; I'll launch the best one inside the venture budget and tell you.",
                                                "reason": "acknowledge the owner"})]])
                done_msgs.add(m["id"])
        # Platform: a proposal came back -> launch the business.
        elif "venture" in g and m["from_kind"] == "agent" and "propos" in body.lower():
            flows.append([[("create_business", {"name": "Columbus AI Receptionists", "template": "voice-agent-sales", "brief": BRIEF,
                                                "reason": "the proposal is well evidenced and fits the venture budget"})],
                          lambda hist: [("reply_human", {"text": "Launched a new business on trial: Columbus AI Receptionists "
                                                          f"({clip(_all_results(hist), 160)}). It sells AI phone receptionists "
                                                          "to local service businesses.", "reason": "tell the owner"})]])
            done_msgs.add(m["id"])
        # Business CEO kickoff: goals backed by real metrics, an offer, and the first tasks.
        elif m["from_kind"] == "system" and body.startswith("Kickoff") and "sales" in g:
            def tasks(hist: list[dict]) -> list[Call]:
                res = _all_results(hist)
                url = re.search(r"https?://\S+?/pay/offer_[0-9a-f]+|https://buy\.stripe\.com/\S+", res)
                offer = (_ids(res, "offer") or [""])[0]
                by = {r["role"]: r for r in reports}
                calls: list[Call] = []
                if "researcher" in by:
                    calls.append(("create_task", {"assignee": by["researcher"]["id"], "title": "Market research and build-vs-white-label decision memo",
                                                  "deliverable": "Decision memo artifact with competitors, pricing, platform costs and a recommendation",
                                                  "reason": "decide how to deliver before scaling"}))
                if "product_engineer" in by:
                    calls.append(("create_task", {"assignee": by["product_engineer"]["id"], "title": "Build the demo receptionist and the sales website",
                                                  "deliverable": "A live demo link and a published home page with the checkout link",
                                                  "description": f"Checkout link: {url.group(0) if url else 'see list_offers'}",
                                                  "reason": "prospects need to see it working"}))
                if "sales" in by:
                    calls.append(("create_task", {"assignee": by["sales"]["id"], "title": "Prospect dental practices in Columbus and start outreach",
                                                  "deliverable": "6+ real leads in the CRM and first personalized emails sent",
                                                  "description": f"Offer {offer}. Lead with the missed-calls problem.",
                                                  "reason": "start the pipeline today"}))
                return calls
            flows.append([[("set_goal", {"owner": agent["id"], "title": "First paying customers", "metric": "paying customers",
                                         "metric_key": "customers", "target": 3, "due": "45", "reason": "the thing to prove"}),
                           ("set_goal", {"owner": agent["id"], "title": "First revenue", "metric": "payments received (USD)",
                                         "metric_key": "revenue_usd", "target": 450, "unit": "USD", "due": "60", "reason": "proof of demand"}),
                           ("create_offer", {"name": "AI Receptionist", "price_usd": 149, "interval": "month",
                                             "description": "An AI phone receptionist that answers every call 24/7, handles FAQs and takes booking requests.",
                                             "reason": "something customers can buy from day one"})],
                          tasks])
            done_msgs.add(m["id"])
        # Customer success: a new customer paid -> welcome email.
        elif m["from_kind"] == "system" and body.startswith("Payment received") and "New customer" in body and agent.get("role") == "customer_success":
            lead = (_ids(body, "lead") or [None])[0]
            cust = (_ids(body, "cust") or [None])[0]
            if lead:
                flows.append([[("send_email", {"lead_id": lead, "kind": "customer", "subject": "Welcome! Let's get your receptionist live",
                                               "body": "Thanks for signing up! To set up your AI receptionist I need: your hours, the services "
                                                       "you offer, how you want booking requests handled, and your 3-5 most common caller questions. "
                                                       "Reply here and you'll be live within 48 hours.", "reason": "start onboarding right away"}),
                               ("update_customer", {"customer_id": cust, "notes": "Welcome sent; waiting for business details.",
                                                    "reason": "track onboarding"})]])
            done_msgs.add(m["id"])
        # Customer success: onboarding details arrived -> build, test, go live.
        elif m["from_kind"] == "system" and body.startswith("Email reply from") and agent.get("role") == "customer_success" and "Hours:" in body:
            lead = (_ids(body, "lead") or [None])[0]
            company = re.search(r"Email reply from (.+?) \[", body)
            hours = re.search(r"Hours: ([^.]+)", body)
            services = re.search(r"Services: ([^.]+)", body)

            def build(hist: list[dict], lead=lead, company=company, hours=hours, services=services) -> list[Call]:
                cust = _customer_for(hist, lead)
                return [("create_voice_agent", {"business_name": company.group(1) if company else "Customer",
                                                "greeting": f"Thanks for calling {company.group(1) if company else 'us'}, how can I help?",
                                                "instructions": "Answer questions, take booking requests, never invent prices.",
                                                "hours": hours.group(1) if hours else "", "services": services.group(1) if services else "",
                                                "booking": "Take name, number and need; the front desk confirms by text.",
                                                "customer_id": cust["id"] if cust else None, "reason": "deliver what they paid for"})]
            flows.append([[("list_customers", {"reason": "find the customer record"})], build,
                          lambda hist: [("test_voice_agent", {"voice_agent_id": (_ids(_all_results(hist), "va") or [""])[0],
                                                              "caller_says": "Hi, are you open Saturday and can I book a cleaning?",
                                                              "reason": "test before going live"})],
                          lambda hist, lead=lead: [("list_customers", {"reason": "confirm ids before go-live"})],
                          lambda hist, lead=lead: _go_live(hist, lead)])
            done_msgs.add(m["id"])
        # Sales: replies from prospects.
        elif m["from_kind"] == "system" and body.startswith("Email reply from") and "crm" in g:
            lead = (_ids(body, "lead") or [None])[0]
            if not lead:
                continue
            if SIGNED.search(body):
                flows.append([[("update_lead", {"lead_id": lead, "notes": "Signed up via the link.", "reason": "log it"})]])
            elif TOO_PRICEY.search(body):
                flows.append([[("update_lead", {"lead_id": lead, "stage": "lost", "notes": "Price above their budget.",
                                                "reason": "honest close-out"})]])
            elif LATER.search(body):
                flows.append([[("update_lead", {"lead_id": lead, "notes": "Asked to check back next quarter.",
                                                "followup_in_days": 60, "reason": "respect their timing"})]])
            elif INTERESTED.search(body):
                flows.append([[("list_offers", {"reason": "pick the right offer"})],
                              lambda hist, lead=lead: [("send_payment_link", {
                                  "lead_id": lead, "offer_id": (_ids(_all_results(hist), "offer") or [""])[0],
                                  "message": "Great question. It's $149/month, no setup fee, cancel anytime. It answers every call "
                                             "24/7, handles your FAQs and takes booking requests. You can start today:",
                                  "reason": "they asked how to buy"})]])
            done_msgs.add(m["id"])
    for t in ctx.get("tasks", []):
        title = t["title"].lower()
        if t["status"] == "blocked":
            continue
        if {"voice", "site"} <= g and ("website" in title or "demo" in title):
            url = re.search(r"https?://\S+", t.get("description") or "")
            flows.append([[("create_voice_agent", {"business_name": "Bright Smiles Dental (demo)",
                                                   "greeting": "Thanks for calling Bright Smiles Dental, how can I help you today?",
                                                   "instructions": "Friendly dental front desk. Take booking requests; never quote treatment prices.",
                                                   "hours": "Mon-Fri 8-5", "services": "cleanings, fillings, crowns, emergencies",
                                                   "reason": "a live demo prospects can try"})],
                          lambda hist, t=t, url=url: [("publish_page", {
                              "path": "index", "title": "AI Receptionist for local businesses",
                              "html": "<h1>Never miss another call</h1><p>An AI receptionist that answers every call 24/7, "
                                      "handles FAQs and takes booking requests. $149/month.</p>"
                                      f"<p><a href='{re.search(r'https?://[^"\s]+/demo/va_[0-9a-f]+', _all_results(hist)).group(0) if re.search(r'/demo/va_', _all_results(hist)) else '#'}'>Try the live demo</a></p>"
                                      f"<p><a href='{url.group(0) if url else '#'}'>Start now</a></p>",
                              "reason": "the sales site"})],
                          lambda hist, t=t: [("update_task", {"task_id": t["id"], "status": "done",
                                                              "result": f"Demo and site live: {clip(_all_results(hist), 200)}", "reason": "shipped"})]
                          + ([("message", {"to": sup["id"], "type": "status", "text": "Demo receptionist and sales site are live.",
                                           "reason": "report up"})] if sup else [])])
            done_tasks.add(t["id"])
        elif "crm" in g and "email" in g and ("prospect" in title or "outreach" in title):
            flows.append([[("find_businesses", {"industry": "dental", "city": "Columbus", "count": 6, "reason": "build the lead list"})],
                          lambda hist: [("add_lead", {**{k: b[k] for k in ("company", "email", "source_url", "contact_name", "website",
                                                                          "city", "industry")}, "reason": "real business, public contact"})
                                        for b in _businesses(hist)],
                          lambda hist: [("send_email", {"lead_id": lid, "kind": "outreach", "subject": "Missed calls at your practice",
                                                        "body": "Hi, when your front desk is busy, calls go to voicemail and most callers "
                                                                "don't leave one. We set up an AI receptionist that answers every call, "
                                                                "handles common questions and takes booking requests. Want me to set up a "
                                                                "free live demo with your practice's details?",
                                                        "reason": "personal, honest first touch"}) for lid in _ids(_all_results(hist), "lead")],
                          lambda hist, t=t: [("update_task", {"task_id": t["id"], "status": "done", "result": "Leads added and first emails sent.",
                                                              "reason": "outreach started"})]])
            done_tasks.add(t["id"])
    return flows, done_msgs, done_tasks


def _businesses(hist: list[dict]) -> list[dict]:
    for r in _last_results(hist):
        try:
            data = json.loads(r)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("businesses"):
            return data["businesses"]
    return []


def _customer_for(hist: list[dict], lead: str | None) -> dict | None:
    for r in _last_results(hist):
        try:
            data = json.loads(r)
        except ValueError:
            continue
        if isinstance(data, list):
            for c in data:
                if isinstance(c, dict) and c.get("lead_id") == lead:
                    return c
    return None


def _go_live(hist: list[dict], lead: str | None) -> list[Call]:
    cust = _customer_for(hist, lead)
    if not cust or not cust.get("voice_agent_id"):
        return [("note_write", {"key": "onboarding", "body": f"Could not find the customer for {lead}.", "reason": "log"})]
    return [("set_voice_agent_status", {"voice_agent_id": cust["voice_agent_id"], "status": "live", "reason": "tested, go live"}),
            ("update_customer", {"customer_id": cust["id"], "status": "live", "notes": "Receptionist live.", "reason": "delivered"}),
            ("send_email", {"lead_id": lead, "kind": "customer", "subject": "You're live!",
                            "body": "Your AI receptionist is live. Forward your line to it whenever you like. "
                                    "Reply here with any changes and I'll update it the same day.", "reason": "tell the customer"})]


def merge(flows: list[list[Turn]]) -> list[Turn]:
    """Run several independent flows side by side: turn k = every flow's k-th step."""
    depth = max((len(f) for f in flows), default=0)
    merged: list[Turn] = []
    for k in range(depth):
        steps = [f[k] for f in flows if k < len(f)]
        merged.append(lambda hist, steps=steps: [c for st in steps for c in (st(hist) if callable(st) else st)])
    return merged


def plan(ctx: dict) -> list[Turn]:
    flows, done_msgs, done_tasks = selling_flows(ctx)
    if flows:
        ctx = {**ctx, "inbox": [m for m in ctx.get("inbox", []) if m["id"] not in done_msgs],
               "tasks": [t for t in ctx.get("tasks", []) if t["id"] not in done_tasks]}
        legacy = legacy_plan(ctx)[:-1]  # without its yield
        turns = merge(flows + ([legacy] if legacy else []))
        return turns + [[("yield_run", {"summary": f"Handled {len(flows)} selling step(s)."})]]
    return legacy_plan(ctx)


def legacy_plan(ctx: dict) -> list[Turn]:
    agent = ctx.get("agent", {})
    grants = agent.get("grants", [])
    reports = (ctx.get("team") or {}).get("reports") or []
    sup = (ctx.get("team") or {}).get("supervisor")
    first: list[Call] = []
    later: list[Turn] = []
    did: list[str] = []

    for m in ctx.get("inbox", []):
        body = m["body"]
        if m["from_kind"] == "system":
            _system(m, agent, grants, reports, first, later, did)
        elif m["from_kind"] == "human":
            if "platform_read" in grants:
                ev = re.search(r"\b(ev_[0-9a-f]+)\b", body)
                first.append(("query_activity", {"event_id": ev.group(1)} if ev else {"limit": 10}))
                first.append(("org_overview", {}))

                def answer(hist: list[dict], mid: str = m["id"], q: str = body) -> list[Call]:
                    res = _last_results(hist)
                    return [("reply_human", {"in_reply_to": mid, "reason": "answer the owner from the log",
                                             "text": f"Re: \"{clip(q, 80)}\"\nFrom the log and ledger:\n"
                                                     f"- recent activity: {clip(res[0] if res else '', 300)}\n"
                                                     f"- orgs: {clip(res[1] if len(res) > 1 else '', 400)}"})]
                later.append(answer)
            else:
                first.append(("reply_human", {"in_reply_to": m["id"], "reason": "acknowledge the owner",
                                              "text": f"Got it. I'll turn this into tasks for the team and report "
                                                      f"back. ({len(ctx.get('tasks', []))} open tasks on my plate.)"}))
                r = _pick_report(reports, body)
                if r and len(body) > 30:
                    first.append(("create_task", {
                        "assignee": r["id"], "title": f"Owner request: {clip(body, 60)}",
                        "deliverable": "A one-page written plan saved with write_artifact",
                        "description": body, "reason": f"delegate to the {r['role']}"}))
            did.append("answered the owner")
        elif m["type"] == "question" and m.get("from_id"):
            first.append(("message", {"to": m["from_id"], "type": "answer", "in_reply_to": m["id"],
                                      "text": "Short answer: go with the cheapest test that gives a real number.",
                                      "reason": "answer a peer's question"}))
        elif m["type"] in ("status", "answer", "recommendation", "escalation"):
            first.append(("note_write", {"key": "inbox-log", "body": f"{m['type']}: {clip(body, 300)}",
                                         "reason": "keep a short log of updates"}))

    for t in ctx.get("tasks", []):
        title = t["title"]
        if t["status"] == "blocked":
            continue
        is_pay = "money" in grants and ctx.get("payees") and re.search(r"\b(pay|buy|register|domain)\b", title, re.I)
        if is_pay:
            payee = ctx["payees"][0]
            first.append(("wallet_balance", {"reason": "check funds before paying"}))
            later.append([("pay", {"payee_id": payee["id"], "amount_usd": 9, "task_id": t["id"],
                                   "memo": f"{title} (1 year)", "reason": "the task requires this purchase"})])
            later.append(lambda hist, tid=t["id"]: [("update_task", {
                "task_id": tid, "status": "done" if "ok" in " ".join(_last_results(hist)).lower()
                or "succeeded" in " ".join(_last_results(hist)) else "blocked",
                "result": clip(" ".join(_last_results(hist)), 300), "reason": "record the payment outcome"})])
            continue
        if re.search(r"\b(contract|sign)\b", title, re.I):
            first.append(("request_human", {"category": "legal_commitment", "task_id": t["id"],
                                            "summary": f"Please sign: {title}", "reason": "only the owner can sign"}))
            continue
        delegate = reports and t["created_by"] != agent.get("id") and t.get("attempts", 0) == 0 \
            and agent.get("role") in ("ceo", "coo", "marketer")
        if delegate:
            r = _pick_report(reports, title + " " + (t.get("description") or ""))
            first.append(("create_task", {"assignee": r["id"], "title": title, "parent_task_id": t["id"],
                                          "deliverable": t["deliverable"], "description": t.get("description", ""),
                                          "reason": f"the {r['role']} owns this kind of work"}))
            first.append(("update_task", {"task_id": t["id"], "status": "done",
                                          "result": f"Delegated to {r['name']}.", "reason": "handed off"}))
            continue
        work: list[Call] = []
        if "web" in grants:
            first.append(("web_search", {"query": clip(title, 80), "reason": "gather evidence for the task"}))

        def finish(hist: list[dict], t: dict = t) -> list[Call]:
            res = _last_results(hist)
            body = (f"# {t['title']}\n\nDeliverable: {t['deliverable']}\n\n"
                    f"## Notes\n{clip(t.get('description') or '', 800)}\n\n"
                    + (f"## Sources (untrusted)\n{clip(res[0], 1200)}\n" if res else "")
                    + "\n_Produced by the dry-run model; no real research was done._\n")
            calls: list[Call] = [
                ("write_artifact", {"name": re.sub(r"\W+", "-", t["title"].lower())[:40] + ".md",
                                    "content": body, "task_id": t["id"], "reason": "save the deliverable"}),
                ("update_task", {"task_id": t["id"], "status": "done", "result": "Deliverable saved as an artifact.",
                                 "reason": "deliverable done"})]
            if sup:
                calls.append(("message", {"to": sup["id"], "type": "status",
                                          "text": f"Done: {t['title']} (artifact saved).", "reason": "report up"}))
            return calls

        if "web" in grants:
            later.append(finish)
        else:
            work.extend(finish([]))
            first.extend(work)
        did.append(f"worked on {clip(title, 40)}")

    if not first and not later and "audit" in grants:
        first.append(("audit_evidence", {"days": 7, "reason": "routine audit sample"}))
        later.append([("note_write", {"key": "summary", "body": "Audit sample reviewed; no new evidence-backed findings.",
                                      "reason": "keep memory current"})])
        did.append("routine audit")
    if not first and not later and "platform_read" in grants and ctx.get("wake_reason") in ("timer", "heartbeat"):
        first.append(("org_overview", {"reason": "routine health check"}))
        later.append([("note_write", {"key": "summary", "body": "Routine check done; nothing alarming.",
                                      "reason": "keep memory current"})])
        did.append("routine health check")

    turns: list[Turn] = []
    if first:
        turns.append(first)
    turns.extend(later)
    summary = "; ".join(did) or "Nothing new to act on."
    turns.append([("yield_run", {"summary": summary})])
    return turns


class DryRunClient:
    name = "dry-run"

    def __init__(self, settings: Settings):
        self.settings = settings

    async def complete(self, *, model: str, system: str, messages: list[dict], tools: list[dict],
                       max_tokens: int, effort: str) -> LLMResponse:
        ctx = _ctx(messages)
        turns = plan(ctx)
        k = sum(1 for m in messages if m["role"] == "assistant")
        turn = turns[min(k, len(turns) - 1)]
        try:
            calls = turn(messages) if callable(turn) else turn
        except Exception as e:  # a scripted step that can't apply: note it and move on
            calls = [("note_write", {"key": "dry-run-skip", "body": f"skipped a scripted step: {type(e).__name__}: {e}"[:300]})]
        allowed = {t["name"] for t in tools}
        content: list[dict[str, Any]] = []
        for i, (name, args) in enumerate(calls):
            if name not in allowed:
                continue
            content.append({"type": "tool_use", "id": f"toolu_dry_{k}_{i}", "name": name, "input": args})
        if not content:
            content = [{"type": "tool_use", "id": f"toolu_dry_{k}_y", "name": "yield_run",
                        "input": {"summary": "Nothing I can do with my tools."}}]
        n_in = (len(system) + len(json.dumps(messages, default=str)) + len(json.dumps(tools))) // 4
        n_out = len(json.dumps(content)) // 4 + 40
        return LLMResponse(content=content, stop_reason="tool_use", input_tokens=n_in, output_tokens=n_out,
                           model="dry-run")

    async def structured(self, *, model: str, system: str, prompt: str, schema: dict,
                         max_tokens: int = 1000) -> tuple[dict, LLMResponse]:
        block = "BLOCKME" in prompt
        data = {"verdict": "block" if block else "allow", "confidence": 0.95 if block else 0.9,
                "reason": "dry-run approver: looks suspicious" if block else "dry-run approver: fits the stated task"}
        return data, LLMResponse(content=[{"type": "text", "text": json.dumps(data)}], stop_reason="end_turn",
                                 input_tokens=len(prompt) // 4, output_tokens=40, model="dry-run")

    async def web_search(self, *, query: str, max_results: int) -> SearchResult:
        slug = re.sub(r"\W+", "-", query.lower()).strip("-")[:40]
        results = [{"title": f"{query} - overview (dry-run)", "url": f"https://example.com/{slug}",
                    "snippet": "Canned result from the dry-run search provider."},
                   {"title": f"{query} - pricing and competitors (dry-run)", "url": f"https://example.org/{slug}",
                    "snippet": "Canned result; no network request was made."}]
        return SearchResult(query=query, results=results[:max_results], provider="dry_run",
                            summary="Dry-run search: no real data.", cost_micros=usd_to_micros(0.01))

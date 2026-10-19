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


# ---------------------------------------------------------------- the owner's research parameters

# Guidance the analyst steers by (text) and hard limits enforced on every research proposal (numbers).
PARAM_TEXT = {
    "focus": "Markets, industries or kinds of business to explore",
    "avoid": "Industries, ideas or business types to stay away from",
    "geography": "Where the customers should be",
    "customer_type": "b2b, b2c or either",
    "business_models": "Preferred business models (e.g. subscriptions, done-for-you services)",
    "risk_tolerance": "low, medium or high",
    "owner_involvement": "How much the owner is willing to do personally",
    "notes": "Anything else the analyst should know",
}
PARAM_LIMITS = {
    "max_startup_usd": "Max startup cost (USD)",
    "max_monthly_cost_usd": "Max monthly running cost (USD)",
    "max_days_to_first_revenue": "Max days to the first payment",
    "min_price_usd": "Min price per sale or per month (USD)",
    "min_month6_profit_usd": "Min projected profit in month 6 (USD)",
}
PARAM_CHOICES = {"customer_type": ("b2b", "b2c", "either"), "risk_tolerance": ("low", "medium", "high")}
PARAMS_KEY = "venture:params"


def get_params(db: Database) -> dict[str, Any]:
    try:
        return json.loads(get_setting(db, PARAMS_KEY, "") or "{}")
    except ValueError:
        return {}


def set_params(db: Database, values: dict[str, Any]) -> dict[str, Any]:
    """Owner-only (dashboard): blank fields are cleared."""
    out: dict[str, Any] = {}
    for k in PARAM_TEXT:
        v = str(values.get(k) or "").strip()[:1000]
        if v and k in PARAM_CHOICES and v.lower() not in PARAM_CHOICES[k]:
            raise PolicyRefusal("params.choice", f"{k} must be one of {', '.join(PARAM_CHOICES[k])}")
        if v:
            out[k] = v.lower() if k in PARAM_CHOICES else v
    for k in PARAM_LIMITS:
        v = values.get(k)
        if v in (None, ""):
            continue
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            raise PolicyRefusal("params.number", f"{PARAM_LIMITS[k]} must be a number")
        if out[k] < 0 and k != "min_month6_profit_usd":
            raise PolicyRefusal("params.number", f"{PARAM_LIMITS[k]} can't be negative")
    set_setting(db, PARAMS_KEY, json.dumps(out))
    log_event(db, actor_kind="human", kind="human", action="venture.params", args=out, outcome="ok")
    return out


def params_text(params: dict[str, Any]) -> str:
    if not params:
        return "(none set: use your judgment, prefer B2B, cheap to test, fast to first payment)"
    lines = [f"- {PARAM_TEXT[k]}: {params[k]}" for k in PARAM_TEXT if params.get(k)]
    lines += [f"- HARD LIMIT, {PARAM_LIMITS[k]}: {params[k]:g}" for k in PARAM_LIMITS if params.get(k) is not None]
    return "\n".join(lines)


def param_violations(case: dict, params: dict[str, Any]) -> list[str]:
    """The owner's hard limits checked against a validated business case."""
    out = []
    lim = params.get
    if lim("max_startup_usd") is not None and case["costs"]["startup_usd"] > lim("max_startup_usd"):
        out.append(f"startup cost ${case['costs']['startup_usd']:,.0f} > the owner's max ${lim('max_startup_usd'):,.0f}")
    if lim("max_monthly_cost_usd") is not None and case["costs"]["monthly_usd"] > lim("max_monthly_cost_usd"):
        out.append(f"monthly cost ${case['costs']['monthly_usd']:,.0f} > the owner's max ${lim('max_monthly_cost_usd'):,.0f}")
    if lim("max_days_to_first_revenue") is not None and case["time_to_first_revenue_days"] > lim("max_days_to_first_revenue"):
        out.append(f"first revenue in {case['time_to_first_revenue_days']:.0f} days > the owner's max "
                   f"{lim('max_days_to_first_revenue'):.0f}")
    if lim("min_price_usd") is not None and case["pricing"]["price_usd"] < lim("min_price_usd"):
        out.append(f"price ${case['pricing']['price_usd']:,.2f} < the owner's min ${lim('min_price_usd'):,.2f}")
    if lim("min_month6_profit_usd") is not None:
        m6 = next((r["profit_usd"] for r in case["projections"] if r["month"] == 6), None)
        if m6 is None or m6 < lim("min_month6_profit_usd"):
            out.append(f"month-6 profit {'not projected' if m6 is None else f'${m6:,.0f}'} < the owner's min "
                       f"${lim('min_month6_profit_usd'):,.0f}")
    return out


def _apply_params(db: Database, case: dict, flags: list[str], source: str) -> None:
    """Research proposals must fit the owner's hard limits; the owner's own ideas may break them (flagged)."""
    bad = param_violations(case, get_params(db))
    if bad and source != "owner":
        raise PolicyRefusal("proposal.params", "outside the owner's research parameters: " + "; ".join(bad))
    flags.extend(f"breaks the owner's own limit: {b}" for b in bad)


# ---------------------------------------------------------------- the business case

PRICE_MODELS = ("one_time", "monthly", "annual", "usage")


def _num(v: Any, name: str, *, lo: float | None = 0, hi: float | None = None) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise PolicyRefusal("proposal.case", f"business_case.{name} must be a number")
    if (lo is not None and x < lo) or (hi is not None and x > hi):
        raise PolicyRefusal("proposal.case", f"business_case.{name} out of range")
    return x


def _text(v: Any, name: str, min_len: int = 1) -> str:
    t = str(v or "").strip()
    if len(t) < min_len:
        raise PolicyRefusal("proposal.case", f"business_case.{name} is missing or too thin")
    return t


def _items(v: Any, name: str, n: int) -> list[dict]:
    if not isinstance(v, list) or len([x for x in v if isinstance(x, dict)]) < n:
        raise PolicyRefusal("proposal.case", f"business_case.{name} needs at least {n} entries")
    return [x for x in v if isinstance(x, dict)]


def validate_case(case: Any, *, starting_budget_usd: float) -> tuple[dict, list[str]]:
    """Check a business case is complete and internally consistent. Returns (clean case, sanity flags).
    Arithmetic is recomputed here, never trusted; the flags are where the red team starts digging."""
    if not isinstance(case, dict):
        raise PolicyRefusal("proposal.case", "a proposal needs a business_case (see the tool schema)")
    pr = case.get("pricing") or {}
    price = _num(pr.get("price_usd"), "pricing.price_usd", lo=0.01)
    model = str(pr.get("model") or "")
    if model not in PRICE_MODELS:
        raise PolicyRefusal("proposal.case", f"business_case.pricing.model must be one of {', '.join(PRICE_MODELS)}")
    comps = _items(pr.get("competitor_prices"), "pricing.competitor_prices", 1)
    if not any(str(c.get("source_url", "")).startswith("http") for c in comps):
        raise PolicyRefusal("proposal.case", "competitor prices need source URLs")
    pricing = {"price_usd": price, "model": model, "rationale": _text(pr.get("rationale"), "pricing.rationale", 20),
               "competitor_prices": comps}
    acq = case.get("acquisition") or {}
    channels = _items(acq.get("channels"), "acquisition.channels", 1)
    for c in channels:
        _text(c.get("channel"), "acquisition.channels[].channel")
        _text(c.get("tactic"), "acquisition.channels[].tactic", 10)
        c["cost_usd"] = _num(c.get("cost_usd", 0), "acquisition.channels[].cost_usd")
        c["expected_conversion_pct"] = _num(c.get("expected_conversion_pct", 0), "acquisition.channels[].expected_conversion_pct",
                                            hi=100)
    acquisition = {"channels": channels,
                   "first_customers": _text(acq.get("first_customers"), "acquisition.first_customers (who exactly the "
                                                                       "first 10 customers are and how we reach them)", 60)}
    mk = case.get("marketing") or {}
    marketing = {"positioning": _text(mk.get("positioning"), "marketing.positioning", 20),
                 "message": _text(mk.get("message"), "marketing.message", 10),
                 "assets": mk.get("assets") or []}
    co = case.get("costs") or {}
    costs = {"startup_usd": _num(co.get("startup_usd", 0), "costs.startup_usd"),
             "monthly_usd": _num(co.get("monthly_usd"), "costs.monthly_usd"),
             "breakdown": _items(co.get("breakdown"), "costs.breakdown", 1)}
    ue = case.get("unit_economics") or {}
    cac = _num(ue.get("cac_usd"), "unit_economics.cac_usd")
    margin = _num(ue.get("gross_margin_pct"), "unit_economics.gross_margin_pct", lo=-100, hi=100)
    ltv = _num(ue.get("ltv_usd"), "unit_economics.ltv_usd")
    ttfr = _num(case.get("time_to_first_revenue_days"), "time_to_first_revenue_days", lo=1)
    proj = _items(case.get("projections"), "projections (monthly: month, customers, revenue_usd, costs_usd)", 3)
    flags: list[str] = []
    rows, cum, breakeven = [], -costs["startup_usd"], None
    prev_rev = None
    for i, r in enumerate(sorted(proj, key=lambda r: float(r.get("month", 0) or 0))):
        m = int(_num(r.get("month", i + 1), "projections[].month", lo=1))
        cust = _num(r.get("customers", 0), "projections[].customers")
        rev = _num(r.get("revenue_usd", 0), "projections[].revenue_usd")
        cst = _num(r.get("costs_usd", 0), "projections[].costs_usd")
        profit = round(rev - cst, 2)
        if r.get("profit_usd") is not None and abs(float(r["profit_usd"]) - profit) > 1:
            flags.append(f"month {m}: stated profit {r['profit_usd']} != revenue - costs = {profit} (corrected)")
        max_rev = cust * price * 1.2  # a month's revenue can't beat paying customers x price (20% slack for usage)
        if rev > max_rev + 1:
            flags.append(f"month {m}: revenue ${rev:,.0f} exceeds customers x price (${max_rev / 1.2:,.0f})")
        if cst < costs["monthly_usd"] * 0.8:
            flags.append(f"month {m}: costs ${cst:,.0f} below the stated monthly run rate ${costs['monthly_usd']:,.0f}")
        if prev_rev and prev_rev > 0 and rev > 3 * prev_rev:
            flags.append(f"month {m}: revenue more than triples month over month")
        prev_rev = rev
        cum = round(cum + profit, 2)
        if breakeven is None and cum >= 0:
            breakeven = m
        rows.append({"month": m, "customers": cust, "revenue_usd": rev, "costs_usd": cst, "profit_usd": profit,
                     "cumulative_profit_usd": cum})
    assumptions = _items(case.get("assumptions"), "assumptions (each with its basis or source)", 3)
    risks = case.get("risks") or []
    if not isinstance(risks, list) or len(risks) < 3:
        raise PolicyRefusal("proposal.case", "business_case.risks needs at least 3 (your own pre-mortem)")
    ratio = (ltv / cac) if cac else None
    if ratio is not None and ratio < 3:
        flags.append(f"LTV/CAC is {ratio:.1f} (healthy is 3+)")
    if cac == 0:
        flags.append("CAC of $0 is not credible: time and tools to acquire customers cost money")
    if margin < 50:
        flags.append(f"gross margin {margin:.0f}% (below 50%)")
    if ttfr > 30:
        flags.append(f"first revenue in {ttfr:.0f} days (slower than 30)")
    if breakeven is None:
        flags.append(f"no break-even within the {len(rows)} projected months")
    elif breakeven > 6:
        flags.append(f"break-even only in month {breakeven}")
    burn = costs["startup_usd"] + costs["monthly_usd"]
    if burn > starting_budget_usd * 1.5:
        flags.append(f"startup + first month cost ${burn:,.0f} vs a ${starting_budget_usd:,.0f}/month trial budget")
    if rows and rows[0]["customers"] >= 5 and ttfr >= 14:
        flags.append("month 1 assumes 5+ customers despite a slow first sale")
    clean = {"pricing": pricing, "acquisition": acquisition, "marketing": marketing, "costs": costs,
             "unit_economics": {"cac_usd": cac, "gross_margin_pct": margin, "ltv_usd": ltv,
                                "ltv_cac": round(ratio, 2) if ratio is not None else None,
                                "payback_months": ue.get("payback_months")},
             "time_to_first_revenue_days": ttfr, "projections": rows, "breakeven_month": breakeven,
             "assumptions": assumptions, "risks": risks}
    return clean, flags


def _norm(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def submit_proposal(db: Database, settings: Settings, *, author: dict | None, fields: dict[str, Any]) -> dict:
    if author is not None and author.get("role") == "red_team":
        raise PolicyRefusal("proposal.independence", "the red team reviews proposals; it doesn't write them")
    for k in ("title", "summary", "thesis", "target_customer", "offer", "plan", "kill_criteria"):
        if not str(fields.get(k, "")).strip():
            raise PolicyRefusal("proposal.incomplete", f"a proposal needs {k}")
    evidence = fields.get("evidence") or []
    if not isinstance(evidence, list) or not any(isinstance(e, dict) and e.get("source_url") for e in evidence):
        raise PolicyRefusal("proposal.evidence", "include evidence with source URLs")
    norm = _norm(str(fields["title"]))
    for other in db.all("SELECT id, title, status FROM proposals WHERE status != 'rejected'"):
        if _norm(other["title"]) == norm:
            raise PolicyRefusal("proposal.duplicate", f"already proposed as {other['id']} ({other['status']}); "
                                                      "propose something different or improve that one")
    budget = float(fields.get("starting_budget_usd") or 50)
    case, flags = validate_case(fields.get("business_case"), starting_budget_usd=budget)
    _apply_params(db, case, flags, fields.get("source") or "research")
    templates = settings.policy.get("venture", {}).get("templates", ["generic"])
    template = fields.get("template") or "generic"
    if template not in templates:
        template = "generic"
    scores = fields.get("scores") or {}
    ts = iso()
    row = {"id": new_id("prop"), "title": fields["title"].strip()[:140], "summary": fields["summary"].strip(),
           "thesis": fields["thesis"].strip(), "target_customer": fields["target_customer"].strip(),
           "offer": fields["offer"].strip(), "evidence_json": json.dumps(evidence), "plan": fields["plan"].strip(),
           "starting_budget_usd": budget, "kill_criteria": fields["kill_criteria"].strip(),
           "template": template, "scores_json": json.dumps(scores), "total_score": score_total(scores),
           "source": fields.get("source") or "research", "source_ref": fields.get("source_ref"), "status": "red_team",
           "org_id": None, "artifact_id": fields.get("artifact_id"), "created_by": author["id"] if author else "human",
           "decided_by": None, "decision_reason": None, "created_at": ts, "updated_at": ts,
           "business_case_json": json.dumps(case), "checks_json": json.dumps(flags), "review_round": 1}
    with db.tx() as conn:
        insert(conn, "proposals", row)
    send_to_red_team(db, settings, row)
    return row


def revise_proposal(db: Database, settings: Settings, *, author: dict | None, proposal_id: str,
                    fields: dict[str, Any], response: str) -> dict:
    """The author answers a 'fix' verdict: update the proposal and send it back for another round."""
    p = db.one("SELECT * FROM proposals WHERE id = ?", (proposal_id,))
    if not p or p["status"] != "revising":
        raise PolicyRefusal("proposal.state", "only a proposal the red team sent back can be revised")
    max_rounds = int(settings.policy.get("venture", {}).get("red_team_max_rounds", 3))
    if p["review_round"] >= max_rounds:
        raise PolicyRefusal("proposal.rounds", f"{max_rounds} review rounds used; it stays parked")
    if not response.strip():
        raise PolicyRefusal("proposal.response", "say how you addressed each required change")
    merged = {**{k: p[k] for k in ("title", "summary", "thesis", "target_customer", "offer", "plan", "kill_criteria",
                                     "starting_budget_usd")},
              "evidence": json.loads(p["evidence_json"]), "scores": json.loads(p["scores_json"]),
              "business_case": json.loads(p["business_case_json"]), **fields}
    budget = float(merged.get("starting_budget_usd") or 50)
    case, flags = validate_case(merged["business_case"], starting_budget_usd=budget)
    _apply_params(db, case, flags, p["source"])
    db.execute("UPDATE proposals SET summary = ?, thesis = ?, target_customer = ?, offer = ?, plan = ?, kill_criteria = ?, "
               "starting_budget_usd = ?, evidence_json = ?, scores_json = ?, total_score = ?, business_case_json = ?, "
               "checks_json = ?, review_round = review_round + 1, status = 'red_team', updated_at = ? WHERE id = ?",
               (merged["summary"], merged["thesis"], merged["target_customer"], merged["offer"], merged["plan"],
                merged["kill_criteria"], budget, json.dumps(merged["evidence"]), json.dumps(merged["scores"]),
                score_total(merged["scores"]), json.dumps(case), json.dumps(flags), iso(), proposal_id))
    p = db.one("SELECT * FROM proposals WHERE id = ?", (proposal_id,))
    send_to_red_team(db, settings, p, response=response)
    return p


# ---------------------------------------------------------------- the red team

RED_TEAM_BRIEF = """Red-team proposal {id} (round {round}): "{title}" (source: {source}).
Your job is to find why this fails before we spend a dollar. Assume the author is optimistic. Attack:
demand (is there proof people PAY for this, not just search for it?), the price (would the target customer
pay it, against the competitors cited?), the first 10 customers (exactly who, reached how, and would they
answer?), the acquisition cost and conversion assumptions, the delivery (can our agents really do it without
the owner?), the costs, the projections, legal and platform risk, and the kill criteria. Do your own
research and cite counter-evidence. Recompute the projections with your own, more conservative numbers.
Also check it fits the owner's research parameters (below); a poor fit is a reason for fix or no_go.
Then call red_team_review with verdict go (launch as is), fix (specific required changes) or no_go.
{owner_note}
The owner's research parameters:
{params}
Code sanity checks already flagged: {flags}

{brief}"""

OWNER_NOTE = ("THIS IS THE OWNER'S OWN IDEA. Be harder on it, not softer: founders fall in love with their ideas, and "
              "the owner asked us explicitly to red-team their ideas hardest. No deference; it needs at least {risks} "
              "risks and {counter} pieces of sourced counter-evidence.")


def _redteam_minimums(source: str) -> tuple[int, int]:
    return (5, 2) if source == "owner" else (3, 1)


def red_team_agent(db: Database) -> dict | None:
    return db.one("SELECT * FROM agents WHERE role = 'red_team' AND status = 'active' ORDER BY created_at LIMIT 1")


def send_to_red_team(db: Database, settings: Settings, p: dict, *, response: str = "") -> None:
    from .engine import create_task
    rt = red_team_agent(db)
    if not rt:
        return  # ensure_red_team runs at startup; maintenance re-sends anything still waiting
    flags = json.loads(p.get("checks_json") or "[]")
    risks, counter = _redteam_minimums(p["source"])
    brief = proposal_brief(p) + (f"\n\nAuthor's response to the last review:\n{response}" if response else "")
    create_task(db, creator=None, assignee_id=rt["id"], title=f"Red-team {p['id']}: {p['title'][:80]}",
                deliverable=f"red_team_review submitted for {p['id']}",
                description=RED_TEAM_BRIEF.format(id=p["id"], round=p["review_round"], title=p["title"], source=p["source"],
                                                  owner_note=OWNER_NOTE.format(risks=risks, counter=counter)
                                                  if p["source"] == "owner" else "",
                                                  flags="; ".join(flags) or "none", brief=brief,
                                                  params=params_text(get_params(db))))


def record_red_team(db: Database, settings: Settings, *, reviewer: dict, proposal_id: str, review: dict[str, Any]) -> dict:
    """Store an independent red-team review. Its scores, not the author's, drive the launch decision."""
    p = db.one("SELECT * FROM proposals WHERE id = ?", (proposal_id,))
    if not p or p["status"] != "red_team":
        raise PolicyRefusal("redteam.state", "this proposal isn't waiting for a red-team review")
    if reviewer.get("role") != "red_team":
        raise PolicyRefusal("redteam.role", "only the red team reviews proposals")
    if reviewer["id"] == p["created_by"]:
        raise PolicyRefusal("redteam.independence", "you can't review your own proposal")
    verdict = review.get("verdict")
    if verdict not in ("go", "fix", "no_go"):
        raise PolicyRefusal("redteam.verdict", "verdict is go, fix or no_go")
    min_risks, min_counter = _redteam_minimums(p["source"])
    risks = [r for r in (review.get("risks") or []) if isinstance(r, dict) and r.get("risk")]
    if len(risks) < min_risks:
        raise PolicyRefusal("redteam.thin", f"name at least {min_risks} concrete risks (likelihood, impact, mitigation)")
    counter = [c for c in (review.get("counter_evidence") or []) if isinstance(c, dict)
               and str(c.get("source_url", "")).startswith("http")]
    if len(counter) < min_counter:
        raise PolicyRefusal("redteam.thin", f"cite at least {min_counter} piece(s) of counter-evidence with source URLs; "
                                            "go find what argues against this")
    challenges = [c for c in (review.get("assumption_challenges") or []) if isinstance(c, dict) and c.get("challenge")]
    if len(challenges) < 3:
        raise PolicyRefusal("redteam.thin", "challenge at least 3 of the author's assumptions, with your revised numbers")
    revised = review.get("revised_scores") or {}
    missing = [k for k in SCORE_WEIGHTS if k not in revised]
    if missing:
        raise PolicyRefusal("redteam.scores", f"re-score every criterion yourself (missing: {', '.join(missing)})")
    rp = review.get("revised_projection") or {}
    for k in ("time_to_first_revenue_days", "month3_profit_usd", "month6_profit_usd"):
        if rp.get(k) is None:
            raise PolicyRefusal("redteam.projection", f"give your own conservative projection ({k})")
    fatal = [f for f in (review.get("fatal_flaws") or []) if str(f).strip()]
    if verdict == "go" and fatal:
        raise PolicyRefusal("redteam.contradiction", "a proposal with fatal flaws can't be a go")
    if verdict == "fix" and not [c for c in (review.get("required_changes") or []) if str(c).strip()]:
        raise PolicyRefusal("redteam.changes", "a 'fix' verdict needs the specific required changes")
    if not str(review.get("summary", "")).strip():
        raise PolicyRefusal("redteam.summary", "summarize the case against it in plain words")
    score = score_total(revised)
    max_rounds = int(settings.policy.get("venture", {}).get("red_team_max_rounds", 3))
    status = {"go": "new", "no_go": "parked", "fix": "revising"}[verdict]
    if verdict == "fix" and p["review_round"] >= max_rounds:
        status = "parked"
    stored = {k: review.get(k) for k in ("summary", "fatal_flaws", "required_changes", "kill_criteria")}
    stored.update({"verdict": verdict, "risks": risks, "counter_evidence": counter, "assumption_challenges": challenges,
                   "revised_scores": revised, "revised_projection": rp, "round": p["review_round"]})
    ts = iso()
    db.execute("UPDATE proposals SET status = ?, redteam_json = ?, redteam_verdict = ?, redteam_score = ?, redteam_by = ?, "
               "redteam_at = ?, decision_reason = CASE WHEN ? = 'parked' THEN ? ELSE decision_reason END, updated_at = ? "
               "WHERE id = ?", (status, json.dumps(stored), verdict, score, reviewer["id"], ts, status,
                                f"red team: {verdict}: {str(review.get('summary'))[:300]}", ts, proposal_id))
    log_event(db, actor_kind="agent", kind="decision", action="proposal.red_team", agent_id=reviewer["id"],
              org_id=reviewer["org_id"], args={"proposal": proposal_id, "round": p["review_round"]},
              result={"verdict": verdict, "score": score, "author_score": p["total_score"]}, outcome="ok",
              ref_type="proposal", ref_id=proposal_id)
    if status == "revising":
        author = db.one("SELECT * FROM agents WHERE id = ?", (p["created_by"],)) if p["created_by"] != "human" else None
        author = author or db.one("SELECT * FROM agents WHERE role = 'venture_analyst' AND status = 'active' LIMIT 1")
        if author:
            post_message(db, settings, org_id=author["org_id"], sender_kind="system", sender_id=None, recipient_kind="agent",
                         recipient_id=author["id"], type="instruction",
                         body=(f"The red team sent proposal [{proposal_id}] '{p['title']}' back (round {p['review_round']}). "
                               f"Required changes: {'; '.join(map(str, review.get('required_changes') or []))}. "
                               f"Their case against it: {review.get('summary')} Fix what's fixable with real evidence (or "
                               "concede the point and lower your numbers), then call revise_proposal with your response."))
    _notify_platform_ceo(db, settings, {**p, "status": status, "redteam_verdict": verdict, "redteam_score": score}, stored)
    return {**p, "status": status, "redteam_verdict": verdict, "redteam_score": score}


def _notify_platform_ceo(db: Database, settings: Settings, p: dict, review: dict) -> None:
    root = root_org(db)
    ceo = org_ceo(db, root["id"]) if root else None
    if not ceo:
        return
    v = settings.policy.get("venture", {})
    auto = v.get("auto_launch", True)
    min_score = float(v.get("auto_launch_min_score", 3.6))
    rp = review.get("revised_projection") or {}
    head = (f"Reviewed proposal [{p['id']}] '{p['title']}' (source: {p['source']}): red team verdict {p['redteam_verdict']}, "
            f"adjusted score {p['redteam_score']}/5 (author {p['total_score']}/5). Red team projects first revenue in "
            f"{rp.get('time_to_first_revenue_days')} days, month-6 profit ${rp.get('month6_profit_usd')}. "
            f"Case against: {review.get('summary')}")
    if p["status"] == "new":
        tail = (f" Standing authorization: launch it with decide_proposal(approve) only if the score is >= {min_score}, the "
                "red team's month-6 profit is positive, and it's within the venture budget; otherwise park it with a reason. "
                "Tell the owner what you decided and the red team's main objections."
                if auto else " The owner decides launches: recommend a decision to the owner with the red team's objections.")
    elif p["status"] == "revising":
        tail = " It went back to the author for changes; nothing to decide yet."
    else:
        tail = (" It's parked. Tell the owner the verdict and the main reasons in plain words"
                + (", candidly: this was the owner's own idea." if p["source"] == "owner" else "."))
    post_message(db, settings, org_id=root["id"], sender_kind="system", sender_id=None, recipient_kind="agent",
                 recipient_id=ceo["id"], type="instruction", body=head + tail)


def red_team_backlog(db: Database, settings: Settings) -> int:
    """Anything waiting for the red team without an open task (e.g. older proposals) gets one."""
    rt = red_team_agent(db)
    if not rt:
        return 0
    n = 0
    for p in db.all("SELECT * FROM proposals WHERE (status = 'red_team') OR (status = 'new' AND redteam_verdict IS NULL)"):
        if db.one("SELECT 1 AS x FROM tasks WHERE assignee_id = ? AND title LIKE ? AND status IN ('open','in_progress','blocked')",
                  (rt["id"], f"Red-team {p['id']}%")):
            continue
        if p["status"] == "new":
            db.execute("UPDATE proposals SET status = 'red_team', review_round = MAX(review_round, 1) WHERE id = ?", (p["id"],))
            p = db.one("SELECT * FROM proposals WHERE id = ?", (p["id"],))
        send_to_red_team(db, settings, p)
        n += 1
    return n


def proposal_brief(p: dict) -> str:
    ev = "\n".join(f"- {e.get('claim', '')} ({e.get('source_url', '')})" for e in json.loads(p["evidence_json"] or "[]"))
    out = (f"{p['summary']}\n\nThesis: {p['thesis']}\nTarget customer: {p['target_customer']}\nOffer: {p['offer']}\n"
           f"Evidence:\n{ev}\n\n60-day plan:\n{p['plan']}\n\nStarting budget: ${p['starting_budget_usd']:.0f}/month\n"
           f"Kill criteria: {p['kill_criteria']}")
    case = json.loads(p.get("business_case_json") or "{}")
    if case:
        out += "\n\nBusiness case:\n" + case_text(case)
    rt = json.loads(p.get("redteam_json") or "null")
    if rt:
        out += "\n\nRed team review (round {r}, verdict {v}):\n{s}\nRisks:\n{risks}\nKill criteria: {k}".format(
            r=rt.get("round"), v=rt.get("verdict"), s=rt.get("summary"), k=rt.get("kill_criteria") or "-",
            risks="\n".join(f"- {x.get('risk')} (likelihood {x.get('likelihood', '?')}, impact {x.get('impact', '?')}; "
                            f"mitigation: {x.get('mitigation', '-')})" for x in rt.get("risks", [])))
    return out


def case_text(c: dict) -> str:
    pr, ue, co = c["pricing"], c["unit_economics"], c["costs"]
    lines = [f"Price: ${pr['price_usd']:,.2f} ({pr['model']}). {pr['rationale']}",
             "Competitors: " + "; ".join(f"{x.get('name', '?')} ${x.get('price', '?')}" for x in pr["competitor_prices"]),
             f"First customers: {c['acquisition']['first_customers']}",
             "Channels: " + "; ".join(f"{x['channel']}: {x['tactic']} (${x['cost_usd']:,.0f}, {x['expected_conversion_pct']}% "
                                      "conversion)" for x in c["acquisition"]["channels"]),
             f"Positioning: {c['marketing']['positioning']} Message: {c['marketing']['message']}",
             f"Costs: ${co['startup_usd']:,.0f} startup, ${co['monthly_usd']:,.0f}/month",
             f"Unit economics: CAC ${ue['cac_usd']:,.0f}, LTV ${ue['ltv_usd']:,.0f} (LTV/CAC {ue['ltv_cac']}), "
             f"gross margin {ue['gross_margin_pct']:.0f}%",
             f"First revenue in {c['time_to_first_revenue_days']:.0f} days; break-even month {c['breakeven_month'] or 'not in range'}",
             "Projection: " + "; ".join(f"m{r['month']}: {r['customers']:.0f} cust, ${r['revenue_usd']:,.0f} rev, "
                                         f"${r['profit_usd']:,.0f} profit" for r in c["projections"])]
    return "\n".join(lines)


def launch_proposal(db: Database, settings: Settings, *, proposal_id: str, actor: dict | None, reason: str = "") -> str:
    """Turn a proposal into a business on trial. Agents are bound by the venture budget; the owner isn't."""
    p = db.one("SELECT * FROM proposals WHERE id = ?", (proposal_id,))
    if not p:
        raise PolicyRefusal("proposal.missing", "no such proposal")
    if p["status"] == "launched":
        raise PolicyRefusal("proposal.launched", f"already launched as {p['org_id']}")
    v = settings.policy.get("venture", {})
    if p["status"] in ("red_team", "revising") or not p["redteam_verdict"]:
        raise PolicyRefusal("proposal.red_team", "the red team hasn't finished reviewing this proposal")
    review = json.loads(p["redteam_json"] or "{}")
    if actor is not None:  # agents launch only what survived the red team; the owner may overrule it
        if p["redteam_verdict"] != "go":
            raise PolicyRefusal("proposal.red_team", f"red team verdict is {p['redteam_verdict']}; only a 'go' can launch")
        min_score = float(v.get("auto_launch_min_score", 3.6))
        if (p["redteam_score"] or 0) < min_score:
            raise PolicyRefusal("proposal.score", f"red-team score {p['redteam_score']} is below {min_score}")
        try:
            m6 = float((review.get("revised_projection") or {}).get("month6_profit_usd"))
        except (TypeError, ValueError):
            m6 = 0.0
        if m6 <= 0:
            raise PolicyRefusal("proposal.profit", "the red team doesn't see a profit by month 6")
        made = int(db.scalar("SELECT COUNT(*) AS n FROM proposals WHERE status = 'launched' AND updated_at >= ? "
                             "AND decided_by != 'human'", (month_start(),)) or 0)
        if made >= int(v.get("max_new_orgs_per_month", 2)):
            raise PolicyRefusal("venture.monthly", "venture budget: max new businesses this month reached")
        if int(db.scalar("SELECT COUNT(*) AS n FROM orgs WHERE status = 'trial'") or 0) >= int(v.get("max_on_trial", 3)):
            raise PolicyRefusal("venture.trial", "too many businesses on trial; review or close one first")
    root = root_org(db)
    pceo = org_ceo(db, root["id"])
    cap = min(float(p["starting_budget_usd"]), float(v.get("starting_cap_usd", 50))) if actor is not None else float(p["starting_budget_usd"])
    note = ("\n\nAlready set from this plan (measured automatically): goals 'First paying customer' and 'Revenue on plan "
            "(first 60 days)'. Add at most 3 more of your own. Watch the red team's risks: they're your early warnings.")
    oid = create_org_from_template(db, settings, template=p["template"], name=p["title"][:60], brief=proposal_brief(p) + note,
                                   parent_org_id=root["id"], supervisor_agent_id=pceo["id"],
                                   created_by=actor["id"] if actor else "human", monthly_cap_usd=cap)
    db.execute("UPDATE orgs SET status = 'trial' WHERE id = ?", (oid,))
    if actor is None and p["redteam_verdict"] != "go":
        reason = f"owner override of red team verdict {p['redteam_verdict']}" + (f": {reason}" if reason else "")
    _seed_goals(db, settings, oid, p, review)
    db.execute("UPDATE proposals SET status = 'launched', org_id = ?, decided_by = ?, decision_reason = ?, updated_at = ? "
               "WHERE id = ?", (oid, actor["id"] if actor else "human", reason, iso(), proposal_id))
    log_event(db, actor_kind="agent" if actor else "human", kind="decision", action="proposal.launch",
              org_id=oid, agent_id=actor["id"] if actor else None, args={"proposal": proposal_id, "reason": reason},
              outcome="ok", ref_type="org", ref_id=oid)
    return oid


def _seed_goals(db: Database, settings: Settings, org_id: str, p: dict, review: dict) -> None:
    """The plan's numbers become measured goals for the new CEO, on the red team's (more conservative) timeline,
    so the trial review compares projection with reality."""
    from .goals import set_goal
    ceo = org_ceo(db, org_id)
    case = json.loads(p["business_case_json"] or "{}")
    if not ceo or not case:
        return
    rp = review.get("revised_projection") or {}
    days = max(float(case.get("time_to_first_revenue_days") or 21), float(rp.get("time_to_first_revenue_days") or 0))
    set_goal(db, settings, setter=None, owner_id=ceo["id"], title="First paying customer", metric="paying customers",
             target=1, unit="customers", due=int(days), metric_key="customers")
    rows = case.get("projections") or []
    two = sum(r["revenue_usd"] for r in rows if r["month"] <= 2)
    if two > 0:
        set_goal(db, settings, setter=None, owner_id=ceo["id"], title="Revenue on plan (first 60 days)",
                 metric="revenue received, USD", target=round(two, 2), unit="USD", due=60, metric_key="revenue_usd")


def decide(db: Database, *, proposal_id: str, status: str, actor: dict | None, reason: str) -> None:
    db.execute("UPDATE proposals SET status = ?, decided_by = ?, decision_reason = ?, updated_at = ? WHERE id = ?",
               (status, actor["id"] if actor else "human", reason, iso(), proposal_id))


def research_cycle(db: Database, settings: Settings, *, force: bool = False) -> bool:
    """Every N days (or now, when the owner asks), give the venture analyst a research task."""
    v = settings.policy.get("venture", {})
    every = float(v.get("research_every_days", 3)) * 86400
    if not force:
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
                deliverable=f"{n} complete proposals submitted with submit_proposal (scored, sourced evidence, full business case)",
                description=("Scan markets, trends and what's working in our portfolio. Prefer B2B offers our agents can "
                             "build, sell by email and deliver themselves, with a first payment possible in 2-3 weeks and "
                             "low legal risk. Skip ideas we already proposed (list_proposals). Score honestly and use "
                             "conservative numbers in the business case: the red team will attack every one.\n\n"
                             "The owner's research parameters (stay inside them; hard limits are enforced on submit):\n"
                             + params_text(get_params(db))))
    return True


OWNER_IDEA = """The owner has an idea for a business. Turn it into a complete proposal with submit_proposal(source='owner'):
research the market yourself, then write the full business case (price and model with sourced competitor prices,
exactly who the first 10 customers are and how we reach them, channels with cost and conversion, positioning and
pitch, costs, CAC / margin / LTV, days to first revenue, monthly projections for months 1-6, assumptions, risks).
Improve the idea where the evidence says so, but don't inflate it to please the owner: the red team reviews owner
ideas hardest. {hints}

Owner's idea:
<owner_idea>
{idea}
</owner_idea>"""


def owner_idea(db: Database, settings: Settings, *, idea: str, name: str = "", template: str = "",
               budget_usd: float | None = None) -> dict | None:
    """The owner's brain-dump goes through the same pipeline as everything else: analyst -> red team -> decision."""
    from .engine import create_task
    analyst = db.one("SELECT * FROM agents WHERE role = 'venture_analyst' AND status = 'active' ORDER BY created_at LIMIT 1")
    if not analyst or not idea.strip():
        return None
    hints = " ".join(x for x in (f"Working name: {name}." if name else "", f"Suggested template: {template}." if template else "",
                                 f"Suggested starting budget: ${budget_usd:.0f}/month." if budget_usd else "") if x)
    return create_task(db, creator=None, assignee_id=analyst["id"], title=f"Owner idea: {(name or idea)[:70]}",
                       deliverable="A proposal submitted with submit_proposal(source='owner') including the full business case",
                       description=OWNER_IDEA.format(hints=hints, idea=idea.strip()[:6000]))

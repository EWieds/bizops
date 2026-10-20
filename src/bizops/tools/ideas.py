"""Ideas pipeline tools: submit (with a full business case), red-team, revise, list and decide proposals."""

from __future__ import annotations

import json

from .. import ideas
from ..engine import PolicyRefusal, root_org
from . import Ctx, ToolOutput, tool

SCORE_PROPS = {k: {"type": "number", "description": "1-5, 5 is best"} for k in ideas.SCORE_WEIGHTS}
NUM = {"type": "number"}
STR = {"type": "string"}
SOURCED = {"type": "array", "items": {"type": "object", "properties": {"claim": STR, "source_url": STR}}}

BUSINESS_CASE = {
    "type": "object",
    "description": "The numbers behind the idea. Checked in code (arithmetic is recomputed) and attacked by the red team, "
                   "so be conservative and source what you can.",
    "properties": {
        "pricing": {"type": "object", "properties": {
            "price_usd": NUM, "model": {"type": "string", "enum": list(ideas.PRICE_MODELS)},
            "rationale": {"type": "string", "description": "why customers pay this, vs. alternatives and their value"},
            "competitor_prices": {"type": "array", "items": {"type": "object", "properties": {
                "name": STR, "price": STR, "source_url": STR}}}}},
        "acquisition": {"type": "object", "properties": {
            "first_customers": {"type": "string", "description": "exactly who the first 10 customers are, where we find "
                                                                  "them and how we reach them"},
            "channels": {"type": "array", "items": {"type": "object", "properties": {
                "channel": STR, "tactic": STR, "cost_usd": NUM, "expected_conversion_pct": NUM}}}}},
        "marketing": {"type": "object", "properties": {
            "positioning": STR, "message": {"type": "string", "description": "the one-line pitch"},
            "assets": {"type": "array", "items": STR}}},
        "costs": {"type": "object", "properties": {
            "startup_usd": NUM, "monthly_usd": NUM,
            "breakdown": {"type": "array", "items": {"type": "object", "properties": {"item": STR, "usd": NUM}}}}},
        "unit_economics": {"type": "object", "properties": {
            "cac_usd": NUM, "gross_margin_pct": NUM, "ltv_usd": NUM, "payback_months": NUM}},
        "time_to_first_revenue_days": NUM,
        "projections": {"type": "array", "description": "monthly, at least months 1-3 (ideally 1-6)",
                        "items": {"type": "object", "properties": {
                            "month": NUM, "customers": NUM, "revenue_usd": NUM, "costs_usd": NUM}}},
        "assumptions": {"type": "array", "items": {"type": "object", "properties": {
            "assumption": STR, "basis": {"type": "string", "description": "source URL or reasoning"}}}},
        "risks": {"type": "array", "items": STR, "description": "your own pre-mortem: how this most likely fails"},
    },
    "required": ["pricing", "acquisition", "marketing", "costs", "unit_economics", "time_to_first_revenue_days",
                 "projections", "assumptions", "risks"],
}

PROPOSAL_PROPS = {
    "title": STR, "summary": {"type": "string", "description": "2-3 sentences"},
    "thesis": STR, "target_customer": STR,
    "offer": {"type": "string", "description": "what we sell and at what price"},
    "evidence": SOURCED, "plan": {"type": "string", "description": "60-day test plan"},
    "starting_budget_usd": NUM, "kill_criteria": STR,
    "template": {"type": "string", "description": "generic, voice-agent-sales, rank-and-rent, digital-products"},
    "scores": {"type": "object", "properties": SCORE_PROPS},
    "business_case": BUSINESS_CASE,
}


@tool("submit_proposal", grant="ideas", action_class="internal", ref_type="proposal",
      description="Submit a new business proposal with its full business case: pricing, how we get the first customers, "
                  "marketing, costs, unit economics, time to first revenue and monthly projections. It goes to the red "
                  "team before anyone can launch it. Scores are 1-5 where 5 is best (for competition, legal_risk and "
                  "owner_steps, 5 means low risk / few steps). Ideas from the owner use source='owner'.",
      properties={**PROPOSAL_PROPS,
                  "source": {"type": "string", "description": "research, owner, youtube"},
                  "source_ref": {"type": "string", "description": "e.g. the YouTube URL or file the idea came from"},
                  "artifact_id": {"type": "string", "description": "the full write-up, if you saved one"}},
      required=["title", "summary", "thesis", "target_customer", "offer", "evidence", "plan", "kill_criteria", "scores",
                "business_case"])
async def submit_proposal(ctx: Ctx, args: dict) -> ToolOutput:
    p = ideas.submit_proposal(ctx.db, ctx.settings, author=ctx.agent, fields=args)
    flags = json.loads(p["checks_json"])
    return ToolOutput({"proposal_id": p["id"], "author_score": p["total_score"], "status": "sent to the red team",
                       "sanity_flags": flags}, ref_type="proposal", ref_id=p["id"])


@tool("revise_proposal", grant="ideas", action_class="internal", ref_type="proposal",
      description="Answer a red-team 'fix' verdict: send the changed fields (anything from submit_proposal, e.g. a new "
                  "business_case) and your point-by-point response. It goes back to the red team.",
      properties={"proposal_id": STR, "response": {"type": "string", "description": "how you addressed each required change"},
                  **{k: v for k, v in PROPOSAL_PROPS.items() if k != "title"}},
      required=["proposal_id", "response"])
async def revise_proposal(ctx: Ctx, args: dict) -> ToolOutput:
    fields = {k: v for k, v in args.items() if k in PROPOSAL_PROPS and k != "title"}
    p = ideas.revise_proposal(ctx.db, ctx.settings, author=ctx.agent, proposal_id=args["proposal_id"], fields=fields,
                              response=args["response"])
    return ToolOutput({"proposal_id": p["id"], "round": p["review_round"], "status": "back with the red team",
                       "sanity_flags": json.loads(p["checks_json"])}, ref_type="proposal", ref_id=p["id"])


@tool("get_proposal", grant="ideas", action_class="internal", ref_type="proposal",
      description="One proposal in full: the brief, business case, code sanity flags and any red-team review.",
      properties={"proposal_id": STR}, required=["proposal_id"])
async def get_proposal(ctx: Ctx, args: dict) -> dict:
    p = ctx.db.one("SELECT * FROM proposals WHERE id = ?", (args["proposal_id"],))
    if not p:
        raise PolicyRefusal("proposal.missing", "no such proposal")
    return {"id": p["id"], "title": p["title"], "status": p["status"], "source": p["source"], "round": p["review_round"],
            "author_score": p["total_score"], "scores": json.loads(p["scores_json"]),
            "red_team_score": p["redteam_score"], "red_team_verdict": p["redteam_verdict"],
            "sanity_flags": json.loads(p["checks_json"] or "[]"),
            "business_case": json.loads(p["business_case_json"] or "{}"),
            "red_team_review": json.loads(p["redteam_json"] or "null"), "brief": ideas.proposal_brief(p)}


@tool("red_team_review", grant="redteam", action_class="internal", ref_type="proposal",
      description="Your independent review of a proposal (you can't review your own). Be adversarial: concrete risks, "
                  "sourced counter-evidence, challenged assumptions with your revised numbers, your own scores and a "
                  "conservative projection. Verdict: go (launch as is), fix (list required_changes; it goes back to the "
                  "author), no_go (parked). A 'go' can't carry fatal flaws. Your scores, not the author's, decide launches.",
      properties={"proposal_id": STR, "verdict": {"type": "string", "enum": ["go", "fix", "no_go"]},
                  "summary": {"type": "string", "description": "the case against it, in plain words"},
                  "fatal_flaws": {"type": "array", "items": STR},
                  "required_changes": {"type": "array", "items": STR},
                  "risks": {"type": "array", "items": {"type": "object", "properties": {
                      "risk": STR, "likelihood": {"type": "string", "enum": ["low", "medium", "high"]},
                      "impact": {"type": "string", "enum": ["low", "medium", "high"]}, "mitigation": STR}}},
                  "counter_evidence": SOURCED,
                  "assumption_challenges": {"type": "array", "items": {"type": "object", "properties": {
                      "assumption": STR, "challenge": STR, "revised_value": STR}}},
                  "revised_scores": {"type": "object", "properties": SCORE_PROPS},
                  "revised_projection": {"type": "object", "properties": {
                      "time_to_first_revenue_days": NUM, "month3_profit_usd": NUM, "month6_profit_usd": NUM,
                      "breakeven_month": NUM}},
                  "kill_criteria": {"type": "string", "description": "sharper, measurable kill criteria"}},
      required=["proposal_id", "verdict", "summary", "risks", "counter_evidence", "assumption_challenges",
                "revised_scores", "revised_projection"])
async def red_team_review(ctx: Ctx, args: dict) -> ToolOutput:
    p = ideas.record_red_team(ctx.db, ctx.settings, reviewer=ctx.agent, proposal_id=args["proposal_id"], review=args)
    return ToolOutput({"proposal_id": p["id"], "verdict": p["redteam_verdict"], "score": p["redteam_score"],
                       "status": p["status"]}, ref_type="proposal", ref_id=p["id"])


@tool("list_proposals", grant="ideas", action_class="internal",
      description="Proposals in the ideas pipeline (red_team, revising, new = reviewed and awaiting a decision, launched, "
                  "parked, rejected).",
      properties={"status": {"type": "string"}})
async def list_proposals(ctx: Ctx, args: dict) -> list:
    sql = ("SELECT id, title, total_score AS author_score, redteam_score, redteam_verdict, status, source, org_id, "
           "decision_reason, created_at FROM proposals")
    params: tuple = ()
    if args.get("status"):
        sql += " WHERE status = ?"
        params = (args["status"],)
    return ctx.db.all(sql + " ORDER BY created_at DESC LIMIT 40", params)


@tool("decide_proposal", grant="venture", action_class="internal", ref_type="proposal", timeout_s=60,
      description="Decide on a red-teamed proposal: approve (launches it as a business on trial; only a red-team 'go' "
                  "with a strong red-team score and positive month-6 profit, within the venture budget), park, or "
                  "reject. Always give the reason.",
      properties={"proposal_id": {"type": "string"}, "decision": {"type": "string", "enum": ["approve", "park", "reject"]},
                  "reason": {"type": "string"}},
      required=["proposal_id", "decision", "reason"])
async def decide_proposal(ctx: Ctx, args: dict) -> ToolOutput:
    root = root_org(ctx.db)
    if not root or ctx.agent["org_id"] != root["id"]:
        raise PolicyRefusal("venture.scope", "only the platform decides proposals")
    if args["decision"] == "approve":
        oid = ideas.launch_proposal(ctx.db, ctx.settings, proposal_id=args["proposal_id"], actor=ctx.agent,
                                    reason=args["reason"])
        return ToolOutput({"launched_org": oid}, ref_type="org", ref_id=oid)
    ideas.decide(ctx.db, proposal_id=args["proposal_id"], status="parked" if args["decision"] == "park" else "rejected",
                 actor=ctx.agent, reason=args["reason"])
    return ToolOutput({"proposal_id": args["proposal_id"], "status": args["decision"]}, ref_type="proposal",
                      ref_id=args["proposal_id"])

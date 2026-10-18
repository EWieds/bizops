"""Ideas pipeline tools: submit, list and decide proposals."""

from __future__ import annotations

import json

from .. import ideas
from ..engine import PolicyRefusal, root_org
from . import Ctx, ToolOutput, tool

SCORE_PROPS = {k: {"type": "number", "description": "1-5, 5 is best"} for k in ideas.SCORE_WEIGHTS}


@tool("submit_proposal", grant="ideas", action_class="internal", ref_type="proposal",
      description="Submit a new business proposal to the ideas pipeline. Be concrete and honest; scores are 1-5 where "
                  "5 is best (for competition, legal_risk and owner_steps, 5 means low risk / few steps).",
      properties={"title": {"type": "string"}, "summary": {"type": "string", "description": "2-3 sentences"},
                  "thesis": {"type": "string"}, "target_customer": {"type": "string"}, "offer": {"type": "string",
                  "description": "what we sell and at what price"},
                  "evidence": {"type": "array", "items": {"type": "object", "properties": {
                      "claim": {"type": "string"}, "source_url": {"type": "string"}}}},
                  "plan": {"type": "string", "description": "60-day test plan"},
                  "starting_budget_usd": {"type": "number"}, "kill_criteria": {"type": "string"},
                  "template": {"type": "string", "description": "generic, voice-agent-sales, rank-and-rent, digital-products"},
                  "scores": {"type": "object", "properties": SCORE_PROPS},
                  "source": {"type": "string", "description": "research, owner, youtube"},
                  "source_ref": {"type": "string", "description": "e.g. the YouTube URL or file the idea came from"},
                  "artifact_id": {"type": "string", "description": "the full write-up, if you saved one"}},
      required=["title", "summary", "thesis", "target_customer", "offer", "evidence", "plan", "kill_criteria", "scores"])
async def submit_proposal(ctx: Ctx, args: dict) -> ToolOutput:
    p = ideas.submit_proposal(ctx.db, ctx.settings, author=ctx.agent, fields=args)
    return ToolOutput({"proposal_id": p["id"], "total_score": p["total_score"]}, ref_type="proposal", ref_id=p["id"])


@tool("list_proposals", grant="ideas", action_class="internal",
      description="Proposals in the ideas pipeline (new, approved, launched, parked, rejected).",
      properties={"status": {"type": "string"}})
async def list_proposals(ctx: Ctx, args: dict) -> list:
    sql = "SELECT id, title, total_score, status, source, org_id, decision_reason, created_at FROM proposals"
    params: tuple = ()
    if args.get("status"):
        sql += " WHERE status = ?"
        params = (args["status"],)
    return ctx.db.all(sql + " ORDER BY created_at DESC LIMIT 40", params)


@tool("decide_proposal", grant="venture", action_class="internal", ref_type="proposal", timeout_s=60,
      description="Decide on a proposal: approve (launches it as a business on trial, within the venture budget), park, "
                  "or reject. Always give the reason.",
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

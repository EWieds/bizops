"""THE gateway. Every tool call and every model call goes through here, in this order:

    1. policy check   (kill switches, grant, hard stops, standing authorization, rate limit)
    2. event log write ('before')
    3. budget check   (budget tree, enforced mid-run)
       + approver pre-screen for risky actions (rules -> Jev -> LLM; allows or blocks, never waits)
    4. the action
    5. event log write ('after') + ledger

No tool may bypass it. Refusals are logged and surface on the dashboard.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from . import budget, ledger
from .db import insert
from .engine import PolicyRefusal, get_agent, get_org, is_runnable
from .events import log_event
from .llm.base import LLMRateLimited, LLMResponse, price_micros, response_cost
from .policy import ALLOW, BLOCK, HARD_STOP, check_action
from .tools import REGISTRY, Ctx, ToolOutput, load_all
from .util import clip, dumps, iso, new_id

RESULT_CLIP = 4000


@dataclass
class ToolResult:
    ok: bool
    content: str
    outcome: str            # ok | refused | error | hard_stop
    cost_micros: int = 0
    call_id: str = ""


class RunStop(Exception):
    """Stop the current run (kill switch or budget). `status` becomes the run status."""

    def __init__(self, status: str, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _as_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, default=str, ensure_ascii=False)


def record_decision(ctx: Ctx, *, call_id: str, action: str, args: dict, verdict: str, rule: str,
                    approver: str | None = None, approver_reason: str | None = None,
                    confidence: float | None = None, task_id: str | None = None,
                    status: str = "closed") -> str:
    did = new_id("pd")
    with ctx.db.tx() as conn:
        insert(conn, "policy_decisions", {
            "id": did, "ts": iso(), "org_id": ctx.agent["org_id"], "agent_id": ctx.agent["id"],
            "run_id": ctx.run_id, "call_id": call_id, "action": action, "args_json": dumps(args),
            "verdict": verdict, "rule": rule, "approver": approver, "approver_reason": approver_reason,
            "confidence": confidence, "task_id": task_id, "status": status,
            "resolved_by": None, "resolution": None, "resolved_at": None,
        })
    return did


async def invoke(ctx: Ctx, name: str, args: dict | None, *, reason: str | None = None) -> ToolResult:
    load_all()
    db, settings = ctx.db, ctx.settings
    args = dict(args or {})
    own_reason = "reason" in ((REGISTRY.get(name).input_schema.get("properties") or {}) if REGISTRY.get(name) else {})
    if reason is None:
        reason = str((args.get("reason") if own_reason else args.pop("reason", "")) or "")
    elif not own_reason:
        args.pop("reason", None)
    reason = reason.strip()
    call_id = new_id("call")
    fresh = get_agent(db, ctx.agent["id"])
    if fresh:
        ctx.agent = fresh
    agent = ctx.agent
    base = dict(org_id=agent["org_id"], agent_id=agent["id"], run_id=ctx.run_id,
                actor_kind=ctx.actor_kind, call_id=call_id, reason=reason or None)

    def refuse(rule: str, detail: str, outcome: str = "refused") -> ToolResult:
        log_event(db, kind="tool", action=name, phase="point", args=args,
                  result={"rule": rule, "detail": detail}, outcome=outcome, **base)
        return ToolResult(False, f"REFUSED by policy ({rule}): {detail}", outcome, call_id=call_id)

    tool = REGISTRY.get(name)
    if tool is None:
        return refuse("tool.unknown", f"no tool named {name!r}")

    # 1. policy
    action_class = tool.classify(args)
    verdict = check_action(db, settings, agent=agent, tool_name=name, grant=tool.grant,
                           action_class=action_class, override=ctx.override)
    if verdict.decision == HARD_STOP:
        task_id = args.get("task_id")
        did = record_decision(ctx, call_id=call_id, action=name, args=args, verdict=HARD_STOP,
                              rule=verdict.rule, task_id=task_id, status="open")
        if task_id and db.one("SELECT 1 AS x FROM tasks WHERE id = ?", (task_id,)):
            db.execute("UPDATE tasks SET status = 'blocked', blocked_reason = ?, updated_at = ? WHERE id = ?",
                       (f"needs a human: {verdict.detail} ({did})", iso(), task_id))
        log_event(db, kind="decision", action=name, phase="point", args=args,
                  result={"rule": verdict.rule, "decision_id": did}, outcome="hard_stop",
                  ref_type="decision", ref_id=did, **base)
        return ToolResult(False, (f"QUEUED FOR A HUMAN ({verdict.rule}): {verdict.detail}. Decision {did}. "
                                  "The task is marked blocked. Do not wait: continue with other work."),
                          "hard_stop", call_id=call_id)
    if verdict.decision == BLOCK:
        if verdict.rule != "kill_switch":
            record_decision(ctx, call_id=call_id, action=name, args=args, verdict=BLOCK, rule=verdict.rule)
        return refuse(verdict.rule, verdict.detail)

    # 2. before event
    log_event(db, kind="tool", action=name, phase="before", args=args, **base)

    # 3. budget
    if tool.cost_estimate is not None:
        est = int(tool.cost_estimate(ctx, args) or 0)
        if est > 0:
            org = get_org(db, agent["org_id"])
            chk = budget.check_spend(db, settings, org=org, agent=agent, amount=est)
            if not chk.ok:
                record_decision(ctx, call_id=call_id, action=name, args=args, verdict=BLOCK, rule=chk.rule)
                log_event(db, kind="tool", action=name, phase="after",
                          result={"rule": chk.rule, "detail": chk.detail}, outcome="refused", **base)
                return ToolResult(False, f"REFUSED by budget ({chk.rule}): {chk.detail}", "refused",
                                  call_id=call_id)

    # 3b. approver pre-screen for risky actions (allows or blocks on its own; never waits)
    approver = ctx.core.approver
    if approver.screens(name) and not ctx.override:
        context = {"agent_kpi": agent.get("kpi_json"), "org_id": agent["org_id"]}
        task_id = args.get("task_id")
        if task_id:
            t = db.one("SELECT title, deliverable, status FROM tasks WHERE id = ?", (task_id,))
            context["task"] = t
        av, trail = await approver.review(agent=agent, tool_name=name, args=args, reason=reason,
                                          context=context)
        for step in trail:
            if step.response is not None:
                _charge_model(ctx, step.response, memo=f"approver ({name})", agent_id=None)
        record_decision(ctx, call_id=call_id, action=name, args=args,
                        verdict=ALLOW if av.verdict == "allow" else BLOCK, rule=f"approver.{av.approver}",
                        approver=" > ".join(f"{s.approver}:{s.verdict}" for s in trail) or av.approver,
                        approver_reason=av.reason, confidence=av.confidence)
        if av.verdict != "allow":
            log_event(db, kind="tool", action=name, phase="after",
                      result={"rule": f"approver.{av.approver}", "detail": av.reason}, outcome="refused", **base)
            return ToolResult(False, f"REFUSED by approver ({av.approver}): {av.reason}", "refused", call_id=call_id)

    # 4. action
    try:
        raw = await asyncio.wait_for(tool.handler(ctx, args), timeout=tool.timeout_s)
    except PolicyRefusal as e:
        if e.rule.startswith("hard_stop"):
            existing = db.one("SELECT id FROM policy_decisions WHERE verdict = 'hard_stop' AND status = 'open' AND rule = ?",
                              (e.rule,))
            did = existing["id"] if existing else record_decision(ctx, call_id=call_id, action=name, args=args,
                                                                  verdict=HARD_STOP, rule=e.rule, status="open")
            log_event(db, kind="decision", action=name, phase="after", result={"rule": e.rule, "decision_id": did},
                      outcome="hard_stop", ref_type="decision", ref_id=did, **base)
            return ToolResult(False, f"QUEUED FOR THE OWNER ({e.rule}): {e.detail} (decision {did}). Continue with other work.",
                              "hard_stop", call_id=call_id)
        record_decision(ctx, call_id=call_id, action=name, args=args, verdict=BLOCK, rule=e.rule)
        log_event(db, kind="tool", action=name, phase="after", result={"rule": e.rule, "detail": e.detail},
                  outcome="refused", **base)
        return ToolResult(False, f"REFUSED ({e.rule}): {e.detail}", "refused", call_id=call_id)
    except TimeoutError:
        log_event(db, kind="tool", action=name, phase="after", result={"error": "timeout"},
                  outcome="error", **base)
        return ToolResult(False, f"ERROR: {name} timed out after {tool.timeout_s}s", "error", call_id=call_id)
    except Exception as e:  # tool bugs must not kill the run; they are logged
        log_event(db, kind="tool", action=name, phase="after",
                  result={"error": f"{type(e).__name__}: {clip(str(e), 500)}"}, outcome="error", **base)
        return ToolResult(False, f"ERROR: {type(e).__name__}: {clip(str(e), 500)}", "error", call_id=call_id)

    out = raw if isinstance(raw, ToolOutput) else ToolOutput(raw)
    text = _as_text(out.content)

    # 5. after event + ledger
    logged = out.content if isinstance(out.content, (dict, list)) and len(text) <= RESULT_CLIP else clip(text, RESULT_CLIP)
    ev = log_event(db, kind="tool", action=name, phase="after", result=logged,
                   outcome="ok", cost_micros=out.cost_micros,
                   ref_type=out.ref_type or tool.ref_type, ref_id=out.ref_id, **base)
    if out.cost_micros and out.record_ledger:
        ledger.record(db, org_id=agent["org_id"], agent_id=agent["id"], run_id=ctx.run_id, kind="spend",
                      amount_micros=out.cost_micros, source="tool", memo=name, event_id=ev)
    return ToolResult(True, text, "ok", out.cost_micros, call_id)


def _charge_model(ctx: Ctx, resp: LLMResponse, *, memo: str, agent_id: str | None) -> int:
    cost = response_cost(ctx.settings, resp)
    if cost:
        ledger.record(ctx.db, org_id=ctx.agent["org_id"], agent_id=agent_id, run_id=ctx.run_id,
                      kind="model_cost", amount_micros=cost, source="model", memo=memo)
    return cost


def estimate_tokens(*parts: Any) -> int:
    return sum(len(p if isinstance(p, str) else json.dumps(p, default=str)) for p in parts) // 4 + 1


async def model_call(ctx: Ctx, *, model: str, system: str, messages: list[dict], tools: list[dict],
                     max_tokens: int, effort: str, turn: int) -> tuple[LLMResponse, int]:
    """A model call through the gateway: kill switch and budget checked before every call."""
    db, settings = ctx.db, ctx.settings
    fresh = get_agent(db, ctx.agent["id"])
    if fresh:
        ctx.agent = fresh
    ok, why = is_runnable(db, ctx.agent)
    if not ok:
        raise RunStop("stopped", why)
    call_id = new_id("call")
    base = dict(org_id=ctx.agent["org_id"], agent_id=ctx.agent["id"], run_id=ctx.run_id,
                actor_kind="agent", call_id=call_id)
    est_in = estimate_tokens(system, messages, tools)
    priced_model = "dry-run" if ctx.core.llm.name == "dry-run" else model
    est = price_micros(settings, priced_model, est_in, max_tokens)
    org = get_org(db, ctx.agent["org_id"])
    chk = budget.check_spend(db, settings, org=org, agent=ctx.agent, amount=est)
    if not chk.ok:
        record_decision(ctx, call_id=call_id, action="llm.call", args={"model": model, "turn": turn},
                        verdict=BLOCK, rule=chk.rule)
        log_event(db, kind="model", action="llm.call", phase="point", args={"model": model, "turn": turn},
                  result={"rule": chk.rule, "detail": chk.detail}, outcome="refused", **base)
        raise RunStop("budget", chk.detail)
    log_event(db, kind="model", action="llm.call", phase="before",
              args={"model": model, "turn": turn, "est_input_tokens": est_in}, **base)
    try:
        resp = await ctx.core.llm.complete(model=model, system=system, messages=messages, tools=tools,
                                           max_tokens=max_tokens, effort=effort)
    except LLMRateLimited as e:
        from .engine import set_setting
        from .util import iso_in
        set_setting(db, "llm_paused_until", iso_in(e.retry_after_s))
        log_event(db, kind="model", action="llm.call", phase="after", result={"rate_limited": str(e)[:300],
                  "retry_after_s": e.retry_after_s}, outcome="refused", **base)
        raise RunStop("rate_limited", f"model provider limit; all agents pause and resume automatically ({str(e)[:160]})")
    except Exception as e:
        log_event(db, kind="model", action="llm.call", phase="after",
                  result={"error": f"{type(e).__name__}: {clip(str(e), 500)}"}, outcome="error", **base)
        raise
    cost = _charge_model(ctx, resp, memo=f"{resp.model} turn {turn}", agent_id=ctx.agent["id"])
    log_event(db, kind="model", action="llm.call", phase="after", cost_micros=cost,
              result={"model": resp.model, "stop_reason": resp.stop_reason, "input_tokens": resp.input_tokens,
                      "output_tokens": resp.output_tokens, "tool_calls": [t["name"] for t in resp.tool_uses],
                      "text": clip(resp.text, 600)},
              outcome="refused" if resp.stop_reason == "refusal" else "ok", **base)
    return resp, cost

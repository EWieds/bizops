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


def plan(ctx: dict) -> list[Turn]:
    agent = ctx.get("agent", {})
    grants = agent.get("grants", [])
    reports = (ctx.get("team") or {}).get("reports") or []
    sup = (ctx.get("team") or {}).get("supervisor")
    first: list[Call] = []
    later: list[Turn] = []
    did: list[str] = []

    for m in ctx.get("inbox", []):
        body = m["body"]
        if m["from_kind"] == "human":
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

    if not first and not later and "platform_read" in grants and ctx.get("wake_reason") == "timer":
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
        calls = turn(messages) if callable(turn) else turn
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

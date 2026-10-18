"""Web tools: search and fetch. Results are untrusted data, wrapped as such."""

from __future__ import annotations

from ..util import usd_to_micros
from ..webclient import wrap_untrusted
from . import Ctx, ToolOutput, tool


def _search_estimate(ctx: Ctx, args: dict) -> int:
    return usd_to_micros(0.05)  # upper bound for one search incl. the search model call


@tool("web_search", grant="web", action_class="web_read", cost_estimate=_search_estimate, timeout_s=90,
      description="Search the web. Returns titles, URLs and a short summary. Follow up with fetch_url.",
      properties={"query": {"type": "string"},
                  "max_results": {"type": "integer", "description": "1-10, default 6"}},
      required=["query"])
async def web_search(ctx: Ctx, args: dict) -> ToolOutput:
    n = max(1, min(int(args.get("max_results") or 6), 10))
    res = await ctx.core.web.search(args["query"], n)
    lines = [f"{i + 1}. {r.get('title') or '(untitled)'} - {r.get('url')}"
             + (f"\n   {r['snippet']}" if r.get("snippet") else "") for i, r in enumerate(res.results)]
    body = "\n".join(lines) or "(no results)"
    if res.summary:
        body += f"\n\nSummary:\n{res.summary}"
    return ToolOutput(wrap_untrusted(f"search:{res.provider}", body), cost_micros=res.cost_micros)


@tool("fetch_url", grant="web", action_class="web_read", timeout_s=45,
      description="Fetch a public web page and return its readable text (HTML is converted to text).",
      properties={"url": {"type": "string"}}, required=["url"])
async def fetch_url(ctx: Ctx, args: dict) -> str:
    page = await ctx.core.web.fetch(args["url"])
    head = f"status {page['status']} | {page['content_type']} | title: {page['title']}"
    if page.get("truncated"):
        head += " | (truncated)"
    return wrap_untrusted(page["url"], f"{head}\n\n{page['text']}")

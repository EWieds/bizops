"""Model client interface. The real client and the scripted dry-run client both implement it.

Messages and content blocks use the Anthropic Messages API shape (plain dicts) everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..config import Settings


class LLMRateLimited(RuntimeError):
    """The model provider says: slow down (subscription session limit, 429...). retry_after_s is a hint."""

    def __init__(self, msg: str, retry_after_s: float = 1800):
        super().__init__(msg)
        self.retry_after_s = retry_after_s


@dataclass
class LLMResponse:
    content: list[dict[str, Any]]
    stop_reason: str
    input_tokens: int
    output_tokens: int
    model: str
    cache_read_tokens: int = 0
    extra_cost_micros: int = 0   # server-side tool fees (e.g. web search)
    cost_override_micros: int | None = None   # exact cost when the backend reports it (claude CLI)

    @property
    def tool_uses(self) -> list[dict[str, Any]]:
        return [b for b in self.content if b.get("type") == "tool_use"]

    @property
    def text(self) -> str:
        return "\n".join(b.get("text", "") for b in self.content if b.get("type") == "text").strip()


@dataclass
class SearchResult:
    query: str
    results: list[dict[str, Any]] = field(default_factory=list)   # {title, url, snippet?}
    summary: str = ""
    cost_micros: int = 0
    provider: str = ""


class LLMClient(Protocol):
    name: str

    async def complete(self, *, model: str, system: str, messages: list[dict], tools: list[dict],
                       max_tokens: int, effort: str) -> LLMResponse: ...

    async def structured(self, *, model: str, system: str, prompt: str, schema: dict,
                         max_tokens: int = 1000) -> tuple[dict, LLMResponse]: ...

    async def web_search(self, *, query: str, max_results: int) -> SearchResult: ...


def response_cost(settings: Settings, resp: LLMResponse) -> int:
    if resp.cost_override_micros is not None:
        return resp.cost_override_micros
    return price_micros(settings, resp.model, resp.input_tokens, resp.output_tokens,
                       resp.cache_read_tokens) + resp.extra_cost_micros


def price_micros(settings: Settings, model: str, input_tokens: int, output_tokens: int,
                 cache_read_tokens: int = 0) -> int:
    """USD micros for a model call. Unknown models are priced at the most expensive tier so
    a typo can never make spend look cheaper than it is."""
    table = settings.models.get("pricing", {})
    if model == "dry-run":
        pin, pout = settings.models.get("dry_run_simulated_pricing", [0, 0])
    elif model in table:
        pin, pout = table[model]
    else:
        pin, pout = max((tuple(v) for v in table.values()), default=(0, 0))
    # Cache reads bill at ~10% of the input rate.
    billable_in = max(input_tokens - cache_read_tokens, 0) + cache_read_tokens * 0.1
    return int(round(billable_in * pin + output_tokens * pout))  # $/MTok * tokens = micros

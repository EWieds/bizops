"""Tool registry. Tools are data-described (name, grant, action class, schema) and are only
ever executed through gateway.invoke. Handlers get a narrow context with no secrets."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..core import Core


@dataclass
class Ctx:
    core: "Core"
    agent: dict
    run_id: str | None = None
    actor_kind: str = "agent"     # agent | human (dashboard override)
    override: bool = False        # a human override from the dashboard skips the approver/rate limits
    yielded: str | None = None    # set by yield_run

    @property
    def db(self):
        return self.core.db

    @property
    def settings(self):
        return self.core.settings


@dataclass
class ToolOutput:
    content: Any
    cost_micros: int = 0
    record_ledger: bool = True    # False when the handler wrote its own ledger entry
    ref_type: str | None = None
    ref_id: str | None = None


Handler = Callable[[Ctx, dict], Awaitable[Any]]


@dataclass
class Tool:
    name: str
    description: str
    grant: str                    # grant group the agent must hold ('core' = everyone)
    action_class: str | Callable[[dict], str]
    input_schema: dict
    handler: Handler
    cost_estimate: Callable[[Ctx, dict], int] | None = None
    timeout_s: float = 60.0
    ref_type: str | None = None

    def classify(self, args: dict) -> str:
        return self.action_class(args) if callable(self.action_class) else self.action_class

    def api_schema(self) -> dict:
        schema = {**self.input_schema}
        props = dict(schema.get("properties", {}))
        props["reason"] = {"type": "string", "description": "One sentence: why you are doing this (shown in the audit log)."}
        schema["properties"] = props
        schema.setdefault("type", "object")
        return {"name": self.name, "description": self.description, "input_schema": schema}


REGISTRY: dict[str, Tool] = {}


def tool(name: str, *, description: str, grant: str, action_class: str | Callable[[dict], str],
         properties: dict | None = None, required: list[str] | None = None,
         cost_estimate: Callable[[Ctx, dict], int] | None = None, timeout_s: float = 60.0,
         ref_type: str | None = None) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        REGISTRY[name] = Tool(
            name=name, description=description.strip(), grant=grant, action_class=action_class,
            input_schema={"type": "object", "properties": properties or {}, "required": required or []},
            handler=fn, cost_estimate=cost_estimate, timeout_s=timeout_s, ref_type=ref_type)
        return fn
    return deco


def tools_for(grants: list[str]) -> list[Tool]:
    load_all()
    return [t for t in REGISTRY.values() if t.grant == "core" or t.grant in grants]


def load_all() -> None:
    from . import comms, files, memory, money, platform, web  # noqa: F401

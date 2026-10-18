"""Policy rules, enforced in code (never by prompt).

Two families:
  * money rules (the v0 guardrail): caps rise only after N complete profitable months, by at
    most X% a month, never above the owner's hard ceiling.
  * action rules: kill switches, tool grants, standing authorizations, hard stops, rate limits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from .config import Settings
from .db import Database
from .engine import grants, is_runnable
from .util import iso, now

ALLOW, BLOCK, HARD_STOP = "allowed", "blocked", "hard_stop"


# ---------------------------------------------------------------- money rules (v0)

def max_allowed_cap(current_cap: int, monthly_profits: list[int], ceiling: int,
                    profitable_months_required: int = 2, max_monthly_increase_pct: int = 25) -> int:
    """Highest cap allowed next month.

    monthly_profits: verified profit for each *complete* month, oldest first. The cap may rise
    only if the last `profitable_months_required` months were all profitable, by at most
    `max_monthly_increase_pct` percent, and never above `ceiling`.
    """
    if current_cap >= ceiling:
        return ceiling
    recent = monthly_profits[-profitable_months_required:] if profitable_months_required else []
    if len(recent) < profitable_months_required or any(p <= 0 for p in recent):
        return current_cap
    raised = current_cap + (current_cap * max_monthly_increase_pct) // 100
    return min(raised, ceiling)


def check_cap_change(current_cap: int, proposed_cap: int, monthly_profits: list[int], ceiling: int,
                     profitable_months_required: int = 2, max_monthly_increase_pct: int = 25) -> tuple[bool, str]:
    if proposed_cap < 0:
        return False, "cap cannot be negative"
    if proposed_cap <= current_cap:
        return True, "decreases are always allowed"
    if proposed_cap > ceiling:
        return False, "above the owner's hard ceiling (hard stop: only the owner can raise the ceiling)"
    limit = max_allowed_cap(current_cap, monthly_profits, ceiling,
                            profitable_months_required, max_monthly_increase_pct)
    if proposed_cap > limit:
        return False, (f"needs {profitable_months_required} complete profitable months and at most "
                       f"+{max_monthly_increase_pct}%/month (max now {limit})")
    return True, "within the profit rule"


# ---------------------------------------------------------------- action rules

@dataclass
class Verdict:
    decision: str            # allowed | blocked | hard_stop
    rule: str
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.decision == ALLOW


def check_action(db: Database, settings: Settings, *, agent: dict, tool_name: str, grant: str,
                 action_class: str, override: bool = False) -> Verdict:
    """Kill switches -> grant -> hard stop -> standing authorization -> rate limit."""
    ok, why = is_runnable(db, agent)
    if not ok:
        return Verdict(BLOCK, "kill_switch", why)
    if grant not in grants(agent) and grant != "core":
        return Verdict(BLOCK, "grant", f"{agent['role']} has no '{grant}' grant for {tool_name}")
    hard = settings.policy.get("hard_stops", {})
    if action_class in hard:
        return Verdict(HARD_STOP, f"hard_stop.{action_class}", hard[action_class])
    auth = settings.policy.get("authorizations", {}).get(action_class)
    if auth != "allow":
        return Verdict(BLOCK, f"authorization.{action_class}",
                       f"no standing authorization for '{action_class}' actions")
    if not override:
        limits = settings.policy.get("rate_limits", {})
        limit = limits.get(tool_name, limits.get("*"))
        if limit is not None:
            since = iso(now() - timedelta(hours=1))
            n = db.scalar("SELECT COUNT(*) AS n FROM events WHERE agent_id = ? AND action = ? "
                          "AND phase = 'before' AND ts >= ?", (agent["id"], tool_name, since))
            if int(n or 0) >= int(limit):
                return Verdict(BLOCK, f"rate_limit.{tool_name}", f"{limit}/hour reached")
    return Verdict(ALLOW, "standing_authorization", action_class)

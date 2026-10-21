"""Budget tree: platform caps -> org caps -> agent budgets. Enforced in code on every
model call and every spend, mid-run. Allocation invariants are checked on every change."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import ledger
from .config import Settings
from .db import Database
from .util import day_start, fmt_usd, month_start, usd_to_micros


@dataclass
class BudgetCheck:
    ok: bool
    rule: str = ""
    detail: str = ""


def platform_caps(settings: Settings) -> tuple[int, int, int]:
    p = settings.policy["platform"]
    ceiling = usd_to_micros(p["hard_ceiling_usd"])
    monthly = min(usd_to_micros(p["monthly_cap_usd"]), ceiling)
    daily = min(usd_to_micros(p["daily_cap_usd"]), monthly)
    return monthly, daily, ceiling


def agent_budgets_enforced(settings: Settings) -> bool:
    """Per-agent monthly budgets are optional (policy budgets.agent_budgets). Off: an agent is limited only by its
    org's monthly cap and the platform caps, so no single agent stalls while its business still has budget."""
    return bool((settings.policy.get("budgets") or {}).get("agent_budgets", False))


def spend_caps_enforced(settings: Settings) -> bool:
    """The platform daily/monthly caps and each org's monthly cap (policy budgets.spend_caps). Off: model use
    and tool spend are unlimited; wallets, purchase limits and payment limits still bound real money."""
    return bool((settings.policy.get("budgets") or {}).get("spend_caps", True))


def check_spend(db: Database | sqlite3.Connection, settings: Settings, *, org: dict,
                agent: dict | None, amount: int) -> BudgetCheck:
    """Would spending `amount` more break any cap in the tree? Pure check, no writes."""
    ms, ds = month_start(), day_start()
    if not spend_caps_enforced(settings):
        if agent is not None and agent_budgets_enforced(settings):
            a_month = ledger.spent(db, since=ms, agent_id=agent["id"])
            if a_month + amount > agent["budget_micros"]:
                return BudgetCheck(False, "agent.budget", f"agent month spend {fmt_usd(a_month)} + {fmt_usd(amount)} > "
                                                          f"budget {fmt_usd(agent['budget_micros'])}")
        return BudgetCheck(True)
    monthly, daily, _ = platform_caps(settings)
    plat_month = ledger.spent(db, since=ms)
    if plat_month + amount > monthly:
        return BudgetCheck(False, "platform.monthly_cap",
                           f"platform month spend {fmt_usd(plat_month)} + {fmt_usd(amount)} > cap {fmt_usd(monthly)}")
    plat_day = ledger.spent(db, since=ds)
    if plat_day + amount > daily:
        return BudgetCheck(False, "platform.daily_cap",
                           f"platform day spend {fmt_usd(plat_day)} + {fmt_usd(amount)} > cap {fmt_usd(daily)}")
    org_month = ledger.spent(db, since=ms, org_id=org["id"])
    if org_month + amount > org["monthly_cap_micros"]:
        return BudgetCheck(False, "org.monthly_cap",
                           f"org month spend {fmt_usd(org_month)} + {fmt_usd(amount)} > cap {fmt_usd(org['monthly_cap_micros'])}")
    if agent is not None and agent_budgets_enforced(settings):
        a_month = ledger.spent(db, since=ms, agent_id=agent["id"])
        if a_month + amount > agent["budget_micros"]:
            return BudgetCheck(False, "agent.budget",
                               f"agent month spend {fmt_usd(a_month)} + {fmt_usd(amount)} > budget {fmt_usd(agent['budget_micros'])}")
    return BudgetCheck(True)


def check_allocation(db: Database, settings: Settings, *, org_id: str | None = None,
                     new_org_cap: int = 0, new_agent_budget: int = 0) -> BudgetCheck:
    """Children can never exceed their parent: sum(agent budgets) <= org cap and
    sum(org caps) <= platform monthly cap <= hard ceiling."""
    if not spend_caps_enforced(settings):
        return BudgetCheck(True)
    monthly, _, ceiling = platform_caps(settings)
    total_caps = int(db.scalar("SELECT COALESCE(SUM(monthly_cap_micros),0) FROM orgs "
                               "WHERE status != 'retired'") or 0)
    if total_caps + new_org_cap > monthly:
        return BudgetCheck(False, "alloc.platform",
                           f"org caps {fmt_usd(total_caps + new_org_cap)} would exceed platform cap {fmt_usd(monthly)}")
    if new_org_cap > ceiling:
        return BudgetCheck(False, "alloc.ceiling", "org cap above hard ceiling")
    if org_id and new_agent_budget:
        org = db.one("SELECT monthly_cap_micros FROM orgs WHERE id = ?", (org_id,))
        used = int(db.scalar("SELECT COALESCE(SUM(budget_micros),0) FROM agents WHERE org_id = ? "
                             "AND status != 'retired'", (org_id,)) or 0)
        if org and used + new_agent_budget > org["monthly_cap_micros"]:
            return BudgetCheck(False, "alloc.org",
                               f"agent budgets {fmt_usd(used + new_agent_budget)} would exceed org cap {fmt_usd(org['monthly_cap_micros'])}")
    return BudgetCheck(True)

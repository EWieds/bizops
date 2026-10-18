"""Money the agents can send, safely.

How money gets in:  only the owner deposits into an org wallet (dashboard or `bizops fund`).
How money gets out: only the `pay` tool, only to payees the owner allowlisted, only inside
  per-payment, per-payee, per-day, wallet and budget limits, after the approver chain, with an
  idempotency key, all logged. Adding a payee or moving money off-platform is a hard stop.

The provider does the actual transfer. `sandbox` (default) records payments without moving
real money. A real provider (e.g. a virtual card or payouts API with its own spend limits)
plugs in behind the same interface, so limits are enforced twice: here and at the issuer.
"""

from __future__ import annotations

from typing import Any, Protocol

from . import ledger
from .config import Settings
from .db import Database, insert
from .events import log_event
from .util import iso, new_id, usd_to_micros


class PaymentProvider(Protocol):
    name: str

    async def send(self, *, payee: dict, amount_micros: int, memo: str, idempotency_key: str) -> dict[str, Any]: ...


class SandboxProvider:
    """Records payments; moves no real money. Safe default for development and trials."""

    name = "sandbox"

    async def send(self, *, payee: dict, amount_micros: int, memo: str, idempotency_key: str) -> dict[str, Any]:
        return {"provider": self.name, "provider_txn": f"sbx_{idempotency_key[-24:]}", "status": "succeeded"}


def make_provider(settings: Settings) -> PaymentProvider:
    name = (settings.secret("BIZOPS_PAYMENTS_PROVIDER") or "sandbox").lower()
    if name == "sandbox":
        return SandboxProvider()
    raise RuntimeError(f"payments provider {name!r} is not implemented yet; use 'sandbox'")


def create_payee(db: Database, *, name: str, org_id: str | None, kind: str = "vendor", provider_ref: str = "",
                 max_per_payment_usd: float = 0, notes: str = "", created_by: str = "human") -> str:
    """Owner-only: allowlist a payee. Agents can only *request* one (a hard stop)."""
    if created_by != "human":
        raise PermissionError("only the owner can add payees")
    pid = new_id("pay")
    with db.tx() as conn:
        insert(conn, "payees", {"id": pid, "org_id": org_id, "name": name, "kind": kind,
                                "provider_ref": provider_ref,
                                "max_per_payment_micros": usd_to_micros(max_per_payment_usd),
                                "status": "active", "notes": notes, "created_by": created_by,
                                "created_at": iso()})
        log_event(conn, actor_kind="human", kind="human", action="payee.create", org_id=org_id,
                  args={"name": name, "max_per_payment_usd": max_per_payment_usd}, outcome="ok",
                  ref_type="payee", ref_id=pid)
    return pid


def deposit(db: Database, *, org_id: str, amount_usd: float, memo: str = "owner deposit") -> str:
    """Owner-only: fund an org wallet (an internal allocation; real cash stays in your accounts)."""
    amount = usd_to_micros(amount_usd)
    if amount <= 0:
        raise ValueError("deposit must be positive")
    with db.tx() as conn:
        ev = log_event(conn, actor_kind="human", kind="human", action="wallet.deposit", org_id=org_id,
                       args={"amount_usd": amount_usd, "memo": memo}, outcome="ok")
        return ledger.record(conn, org_id=org_id, kind="deposit", amount_micros=amount, source="human",
                             memo=memo, event_id=ev)

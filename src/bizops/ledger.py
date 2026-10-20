"""Append-only money ledger.

Kinds and who may write them:
  model_cost, spend       system (gateway) only
  payment, payment_reversal   the pay tool via the payment provider
  deposit                 human only (owner funds an org wallet)
  revenue                 connector or human only (or the sandbox, kept separate); agents can never write it
  revenue_reversal        refunds and chargebacks, same writers as revenue; revenue() is always net of them
"""

from __future__ import annotations

import sqlite3

from .db import Database, insert
from .util import iso, new_id

SPEND_KINDS = ("model_cost", "spend", "payment")
CREDIT_KINDS = ("payment_reversal",)

ALLOWED_SOURCES = {
    "model_cost": {"model"},
    "spend": {"system", "tool"},
    "payment": {"provider"},
    "payment_reversal": {"provider", "human"},
    "deposit": {"human"},
    "revenue": {"connector", "human", "sandbox"},
    "revenue_reversal": {"connector", "human", "sandbox"},
}


class LedgerError(ValueError):
    pass


def record(target: Database | sqlite3.Connection, *, org_id: str, kind: str, amount_micros: int,
           source: str, agent_id: str | None = None, run_id: str | None = None,
           memo: str | None = None, ref_id: str | None = None, event_id: str | None = None) -> str:
    if kind not in ALLOWED_SOURCES:
        raise LedgerError(f"unknown ledger kind {kind!r}")
    if source not in ALLOWED_SOURCES[kind]:
        raise LedgerError(f"source {source!r} may not write {kind!r}")
    if amount_micros < 0:
        raise LedgerError("amounts are positive; the kind gives the direction")
    row = {
        "id": new_id("le"), "ts": iso(), "org_id": org_id, "agent_id": agent_id,
        "run_id": run_id, "kind": kind, "amount_micros": int(amount_micros), "source": source,
        "memo": memo, "ref_id": ref_id, "event_id": event_id,
    }
    if isinstance(target, Database):
        with target.tx() as conn:
            insert(conn, "ledger_entries", row)
    else:
        insert(target, "ledger_entries", row)
    return row["id"]


def _q(target: Database | sqlite3.Connection, sql: str, params: tuple) -> int:
    if isinstance(target, Database):
        return int(target.scalar(sql, params) or 0)
    row = target.execute(sql, params).fetchone()
    return int(row[0] or 0) if row else 0


def spent(target: Database | sqlite3.Connection, *, since: str, org_id: str | None = None,
          agent_id: str | None = None) -> int:
    """Net spend (model cost + spend + payments - reversals) since a timestamp."""
    where, params = ["ts >= ?"], [since]
    if org_id:
        where.append("org_id = ?")
        params.append(org_id)
    if agent_id:
        where.append("agent_id = ?")
        params.append(agent_id)
    sql = ("SELECT COALESCE(SUM(CASE WHEN kind IN ('model_cost','spend','payment') THEN amount_micros "
           "WHEN kind = 'payment_reversal' THEN -amount_micros ELSE 0 END), 0) "
           f"FROM ledger_entries WHERE {' AND '.join(where)}")
    return _q(target, sql, tuple(params))


def wallet_balance(target: Database | sqlite3.Connection, org_id: str) -> int:
    sql = ("SELECT COALESCE(SUM(CASE WHEN kind IN ('deposit','payment_reversal') THEN amount_micros "
           "WHEN kind = 'payment' THEN -amount_micros ELSE 0 END), 0) "
           "FROM ledger_entries WHERE org_id = ?")
    return _q(target, sql, (org_id,))


def payments_since(target: Database | sqlite3.Connection, org_id: str, since: str) -> int:
    sql = ("SELECT COALESCE(SUM(CASE WHEN kind = 'payment' THEN amount_micros "
           "WHEN kind = 'payment_reversal' THEN -amount_micros ELSE 0 END), 0) "
           "FROM ledger_entries WHERE org_id = ? AND ts >= ?")
    return _q(target, sql, (org_id, since))


def revenue(target: Database | sqlite3.Connection, *, since: str, org_id: str | None = None,
            sandbox: bool = False) -> int:
    """Net revenue (minus refunds and chargebacks) since a timestamp. Real revenue (connector/human) and
    sandbox revenue never mix."""
    where = "kind IN ('revenue','revenue_reversal') AND ts >= ? AND source " + ("= 'sandbox'" if sandbox else "IN ('connector','human')")
    params: list = [since]
    if org_id:
        where += " AND org_id = ?"
        params.append(org_id)
    return _q(target, "SELECT COALESCE(SUM(CASE WHEN kind = 'revenue' THEN amount_micros ELSE -amount_micros END), 0) "
              f"FROM ledger_entries WHERE {where}", tuple(params))

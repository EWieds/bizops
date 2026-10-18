"""Append-only audit log. Nothing happens off the log.

There is deliberately no update or delete function here.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .db import Database, insert
from .util import dumps, iso, new_id


def log_event(target: Database | sqlite3.Connection, *, actor_kind: str, kind: str, action: str,
              phase: str = "point", org_id: str | None = None, agent_id: str | None = None,
              run_id: str | None = None, call_id: str | None = None, reason: str | None = None,
              args: Any = None, result: Any = None, outcome: str | None = None,
              cost_micros: int = 0, ref_type: str | None = None, ref_id: str | None = None) -> str:
    row = {
        "id": new_id("ev"),
        "ts": iso(),
        "org_id": org_id,
        "agent_id": agent_id,
        "run_id": run_id,
        "actor_kind": actor_kind,
        "kind": kind,
        "action": action,
        "phase": phase,
        "call_id": call_id,
        "reason": reason,
        "args_json": None if args is None else dumps(args),
        "result_json": None if result is None else dumps(result),
        "outcome": outcome,
        "cost_micros": int(cost_micros or 0),
        "ref_type": ref_type,
        "ref_id": ref_id,
    }
    if isinstance(target, Database):
        with target.tx() as conn:
            insert(conn, "events", row)
    else:
        insert(target, "events", row)
    return row["id"]

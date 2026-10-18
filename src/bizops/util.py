"""Small shared helpers: ids, time, money, json."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any

MICROS = 1_000_000


def new_id(prefix: str) -> str:
    """Sortable id: prefix + 12 hex chars of ms timestamp + 10 random hex chars."""
    return f"{prefix}_{int(time.time() * 1000):012x}{os.urandom(5).hex()}"


def now() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def iso_in(seconds: float) -> str:
    return iso(now() + timedelta(seconds=seconds))


def month_start(dt: datetime | None = None) -> str:
    d = (dt or now()).astimezone(UTC)
    return iso(d.replace(day=1, hour=0, minute=0, second=0, microsecond=0))


def day_start(dt: datetime | None = None) -> str:
    d = (dt or now()).astimezone(UTC)
    return iso(d.replace(hour=0, minute=0, second=0, microsecond=0))


def usd_to_micros(usd: float | int | str) -> int:
    return int(round(float(usd) * MICROS))


def fmt_usd(micros: int | None) -> str:
    m = micros or 0
    if 0 < abs(m) < 10_000:  # under a cent: show more precision
        return f"${m / MICROS:.4f}"
    return f"${m / MICROS:,.2f}"


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False, sort_keys=True)


def loads(s: str | None, default: Any = None) -> Any:
    if s is None or s == "":
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default


def clip(text: str | None, n: int) -> str:
    t = text or ""
    return t if len(t) <= n else t[: n - 1] + "…"

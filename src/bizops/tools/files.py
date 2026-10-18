"""Artifacts: files an agent produced. Sandboxed to data/artifacts/<org>/."""

from __future__ import annotations

import hashlib
import mimetypes
import re

from ..db import insert
from ..engine import PolicyRefusal
from ..util import iso, new_id
from . import Ctx, ToolOutput, tool

MAX_BYTES = 1_000_000


@tool("write_artifact", grant="files", action_class="internal", ref_type="artifact",
      description="Save a file you produced (markdown, csv, html, json, txt). Returns its artifact id.",
      properties={"name": {"type": "string", "description": "file name, e.g. plan.md"},
                  "content": {"type": "string"},
                  "task_id": {"type": "string"}},
      required=["name", "content"])
async def write_artifact(ctx: Ctx, args: dict) -> ToolOutput:
    data = args["content"].encode()
    if len(data) > MAX_BYTES:
        raise PolicyRefusal("files.size", f"max {MAX_BYTES} bytes")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", args["name"]).strip("._")[:80] or "file.txt"
    aid = new_id("art")
    rel = f"{ctx.agent['org_id']}/{aid}-{safe}"
    path = ctx.settings.artifacts_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    mime = mimetypes.guess_type(safe)[0] or "text/plain"
    task_id = args.get("task_id")
    if task_id and not ctx.db.one("SELECT 1 AS x FROM tasks WHERE id = ?", (task_id,)):
        task_id = None
    with ctx.db.tx() as conn:
        insert(conn, "artifacts", {"id": aid, "org_id": ctx.agent["org_id"], "agent_id": ctx.agent["id"],
                                   "task_id": task_id, "name": safe, "path": rel, "mime": mime,
                                   "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                                   "created_at": iso()})
    return ToolOutput({"artifact_id": aid, "name": safe, "bytes": len(data)}, ref_type="artifact", ref_id=aid)


@tool("read_artifact", grant="files", action_class="internal",
      description="Read a file produced in your org by artifact id.",
      properties={"artifact_id": {"type": "string"}}, required=["artifact_id"])
async def read_artifact(ctx: Ctx, args: dict) -> str:
    row = ctx.db.one("SELECT * FROM artifacts WHERE id = ? AND org_id = ?", (args["artifact_id"], ctx.agent["org_id"]))
    if not row:
        raise PolicyRefusal("files.missing", "no such artifact in your org")
    text = (ctx.settings.artifacts_dir / row["path"]).read_text(errors="replace")
    return text[:20000]

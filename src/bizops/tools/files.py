"""Artifacts: files an agent produced. Sandboxed to data/artifacts/<org>/."""

from __future__ import annotations

import hashlib
import mimetypes
import re

from ..db import insert
from ..engine import PolicyRefusal, org_filter
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


@tool("list_artifacts", grant="files", action_class="internal",
      description="List files produced in your org (newest first), optionally filtered by name or task. "
                  "org_id lists another org's files you can read (the platform's; any, for platform executives).",
      properties={"name_contains": {"type": "string"}, "task_id": {"type": "string"}, "org_id": {"type": "string"}})
async def list_artifacts(ctx: Ctx, args: dict) -> list:
    org_id = args.get("org_id") or ctx.agent["org_id"]
    where, wparams = org_filter(ctx.db, ctx.agent, "ar.org_id")
    sql = ("SELECT ar.id, ar.name, ar.task_id, ar.size_bytes, ar.created_at, a.name AS author FROM artifacts ar "
           f"LEFT JOIN agents a ON a.id = ar.agent_id WHERE ar.org_id = ? AND {where}")
    params: list = [org_id, *wparams]
    if args.get("name_contains"):
        sql += " AND ar.name LIKE ?"
        params.append(f"%{args['name_contains']}%")
    if args.get("task_id"):
        sql += " AND ar.task_id = ?"
        params.append(args["task_id"])
    return ctx.db.all(sql + " ORDER BY ar.created_at DESC LIMIT 30", tuple(params))


@tool("read_artifact", grant="files", action_class="internal",
      description="Read a file by artifact id or file name (latest version): your org's files, the platform's, "
                  "and (platform executives) any business's.",
      properties={"artifact_id": {"type": "string", "description": "art_... id, or a file name like plan.md"}},
      required=["artifact_id"])
async def read_artifact(ctx: Ctx, args: dict) -> str:
    key = args["artifact_id"].strip()
    where, params = org_filter(ctx.db, ctx.agent)
    row = ctx.db.one(f"SELECT * FROM artifacts WHERE id = ? AND {where}", (key, *params))
    if not row:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", key).strip("._")
        row = ctx.db.one(f"SELECT * FROM artifacts WHERE (name = ? OR name = ?) AND {where} "
                         "ORDER BY org_id != ?, created_at DESC LIMIT 1", (key, safe, *params, ctx.agent["org_id"]))
    if not row:
        raise PolicyRefusal("files.missing", "no such file you can read; use list_artifacts")
    text = (ctx.settings.artifacts_dir / row["path"]).read_text(errors="replace")
    if row["org_id"] != ctx.agent["org_id"]:
        org = ctx.db.one("SELECT name FROM orgs WHERE id = ?", (row["org_id"],))
        text = f"(file of {org['name'] if org else row['org_id']})\n{text}"
    return text[:20000]

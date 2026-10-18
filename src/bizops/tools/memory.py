"""Memory: per-agent notes and the org knowledge base. Summarize, don't append forever."""

from __future__ import annotations

from ..db import insert
from ..engine import PolicyRefusal
from ..util import iso, new_id
from . import Ctx, tool

MAX_NOTE_CHARS = 4000


def _upsert(ctx: Ctx, agent_id: str | None, key: str, body: str) -> str:
    if len(body) > MAX_NOTE_CHARS:
        raise PolicyRefusal("memory.size", f"notes are capped at {MAX_NOTE_CHARS} chars: summarize")
    key = key.strip()[:80] or "note"
    with ctx.db.tx() as conn:
        if agent_id is None:
            row = conn.execute("SELECT id FROM memory_notes WHERE org_id = ? AND agent_id IS NULL AND key = ?",
                               (ctx.agent["org_id"], key)).fetchone()
        else:
            row = conn.execute("SELECT id FROM memory_notes WHERE agent_id = ? AND key = ?",
                               (agent_id, key)).fetchone()
        if row:
            conn.execute("UPDATE memory_notes SET body = ?, updated_at = ? WHERE id = ?", (body, iso(), row[0]))
            return row[0]
        nid = new_id("mem")
        insert(conn, "memory_notes", {"id": nid, "org_id": ctx.agent["org_id"], "agent_id": agent_id,
                                      "key": key, "body": body, "created_at": iso(), "updated_at": iso()})
        return nid


@tool("note_write", grant="memory", action_class="internal",
      description="Write (or overwrite) one of your private notes by key. Keep a 'summary' note current.",
      properties={"key": {"type": "string"}, "body": {"type": "string"}}, required=["key", "body"])
async def note_write(ctx: Ctx, args: dict) -> dict:
    return {"note": _upsert(ctx, ctx.agent["id"], args["key"], args["body"])}


@tool("note_delete", grant="memory", action_class="internal",
      description="Delete one of your private notes (to keep memory small).",
      properties={"key": {"type": "string"}}, required=["key"])
async def note_delete(ctx: Ctx, args: dict) -> dict:
    ctx.db.execute("DELETE FROM memory_notes WHERE agent_id = ? AND key = ?", (ctx.agent["id"], args["key"]))
    return {"deleted": args["key"]}


@tool("note_read", grant="memory", action_class="internal",
      description="Read your notes (all, or one key).", properties={"key": {"type": "string"}})
async def note_read(ctx: Ctx, args: dict) -> list:
    if args.get("key"):
        return ctx.db.all("SELECT key, body, updated_at FROM memory_notes WHERE agent_id = ? AND key = ?",
                          (ctx.agent["id"], args["key"]))
    return ctx.db.all("SELECT key, body, updated_at FROM memory_notes WHERE agent_id = ? ORDER BY key",
                      (ctx.agent["id"],))


@tool("kb_write", grant="memory", action_class="internal",
      description="Write an entry in the org knowledge base (shared with your whole org).",
      properties={"key": {"type": "string"}, "body": {"type": "string"}}, required=["key", "body"])
async def kb_write(ctx: Ctx, args: dict) -> dict:
    return {"kb": _upsert(ctx, None, args["key"], args["body"])}


@tool("kb_read", grant="memory", action_class="internal",
      description="Read the org knowledge base (all keys, or one entry).", properties={"key": {"type": "string"}})
async def kb_read(ctx: Ctx, args: dict) -> list:
    if args.get("key"):
        return ctx.db.all("SELECT key, body, updated_at FROM memory_notes WHERE org_id = ? AND agent_id IS NULL "
                          "AND key = ?", (ctx.agent["org_id"], args["key"]))
    return ctx.db.all("SELECT key, substr(body, 1, 200) AS preview, updated_at FROM memory_notes "
                      "WHERE org_id = ? AND agent_id IS NULL ORDER BY key", (ctx.agent["org_id"],))

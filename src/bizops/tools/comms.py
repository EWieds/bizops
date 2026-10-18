"""Communication tools: messages, advice up, escalation, tasks, chat replies, human queue."""

from __future__ import annotations

from ..engine import (MESSAGE_TYPES, PolicyRefusal, check_message_route, create_task, get_agent,
                      post_message, team, update_task)
from . import Ctx, ToolOutput, tool


def _send(ctx: Ctx, to_id: str, type_: str, text: str, in_reply_to: str | None = None) -> ToolOutput:
    recipient = get_agent(ctx.db, to_id)
    if not recipient:
        raise PolicyRefusal("message.recipient", f"no agent {to_id}")
    check_message_route(ctx.db, ctx.agent, recipient)
    msg = post_message(ctx.db, ctx.settings, org_id=ctx.agent["org_id"], sender_kind="agent",
                       sender_id=ctx.agent["id"], recipient_kind="agent", recipient_id=to_id,
                       type=type_, body=text, in_reply_to=in_reply_to)
    return ToolOutput({"sent": msg["id"], "to": recipient["name"], "hop": msg["hop"]},
                      ref_type="message", ref_id=msg["id"])


@tool("message", grant="comms", action_class="internal",
      description="Send a message to another agent in your org (or your direct supervisor/reports). "
                  "Use in_reply_to to answer a message. Push back, advise and pass information to peers.",
      properties={"to": {"type": "string", "description": "agent id"},
                  "type": {"type": "string", "enum": [t for t in MESSAGE_TYPES if t != "chat"]},
                  "text": {"type": "string"},
                  "in_reply_to": {"type": "string", "description": "message id you are answering"}},
      required=["to", "type", "text"])
async def message(ctx: Ctx, args: dict) -> ToolOutput:
    return _send(ctx, args["to"], args["type"], args["text"], args.get("in_reply_to"))


@tool("recommend_up", grant="comms", action_class="internal",
      description="Give advice or a recommendation to your supervisor.",
      properties={"text": {"type": "string"}}, required=["text"])
async def recommend_up(ctx: Ctx, args: dict) -> ToolOutput:
    if not ctx.agent["parent_agent_id"]:
        raise PolicyRefusal("message.no_supervisor", "you have no supervisor; tell the owner with reply_human")
    return _send(ctx, ctx.agent["parent_agent_id"], "recommendation", args["text"])


@tool("escalate", grant="comms", action_class="internal",
      description="Escalate a problem you cannot solve to your supervisor.",
      properties={"text": {"type": "string"}}, required=["text"])
async def escalate(ctx: Ctx, args: dict) -> ToolOutput:
    if not ctx.agent["parent_agent_id"]:
        raise PolicyRefusal("message.no_supervisor", "you have no supervisor; use request_human for hard stops")
    return _send(ctx, ctx.agent["parent_agent_id"], "escalation", args["text"])


@tool("create_task", grant="comms", action_class="internal", ref_type="task",
      description="Assign a task to yourself or an agent below you. The deliverable must be checkable.",
      properties={"assignee": {"type": "string", "description": "agent id"},
                  "title": {"type": "string"},
                  "deliverable": {"type": "string", "description": "what done means, checkably"},
                  "description": {"type": "string"},
                  "parent_task_id": {"type": "string"}},
      required=["assignee", "title", "deliverable"])
async def create_task_tool(ctx: Ctx, args: dict) -> ToolOutput:
    t = create_task(ctx.db, creator=ctx.agent, assignee_id=args["assignee"], title=args["title"],
                    deliverable=args["deliverable"], description=args.get("description", ""),
                    parent_task_id=args.get("parent_task_id"))
    return ToolOutput({"task_id": t["id"], "assignee": args["assignee"]}, ref_type="task", ref_id=t["id"])


@tool("update_task", grant="comms", action_class="internal", ref_type="task",
      description="Update the status of your task (or a report's task): in_progress, done, blocked, failed, cancelled.",
      properties={"task_id": {"type": "string"},
                  "status": {"type": "string", "enum": ["in_progress", "done", "blocked", "failed", "cancelled"]},
                  "result": {"type": "string", "description": "what you produced; reference artifact ids"},
                  "blocked_reason": {"type": "string"}},
      required=["task_id", "status"])
async def update_task_tool(ctx: Ctx, args: dict) -> ToolOutput:
    t = update_task(ctx.db, actor=ctx.agent, task_id=args["task_id"], status=args["status"],
                    result=args.get("result"), blocked_reason=args.get("blocked_reason"))
    return ToolOutput({"task_id": t["id"], "status": t["status"]}, ref_type="task", ref_id=t["id"])


@tool("reply_human", grant="comms", action_class="internal",
      description="Reply to the owner in the dashboard chat. Only org CEOs talk to the owner.",
      properties={"text": {"type": "string"},
                  "in_reply_to": {"type": "string", "description": "the owner's message id"}},
      required=["text"])
async def reply_human(ctx: Ctx, args: dict) -> ToolOutput:
    if ctx.agent["depth"] != 0:
        raise PolicyRefusal("chat.ceo_only", "only an org CEO replies to the owner; send it up instead")
    msg = post_message(ctx.db, ctx.settings, org_id=ctx.agent["org_id"], sender_kind="agent",
                       sender_id=ctx.agent["id"], recipient_kind="human", recipient_id=None,
                       type="answer", body=args["text"], in_reply_to=args.get("in_reply_to"))
    return ToolOutput({"sent": msg["id"]}, ref_type="message", ref_id=msg["id"])


HARD_STOP_CATEGORIES = ["legal_commitment", "identity_account", "consumer_voice_sms", "raise_cap",
                        "external_transfer"]


@tool("request_human", grant="comms", action_class=lambda a: str(a.get("category", "legal_commitment")),
      description="File an item the system legally cannot do for you (signing contracts, identity-verified "
                  "accounts, consumer calls/texts without consent, raising caps past the ceiling, moving money "
                  "out). It goes to the owner's queue; the task is marked blocked; keep working on other things.",
      properties={"category": {"type": "string", "enum": HARD_STOP_CATEGORIES},
                  "summary": {"type": "string", "description": "exactly what you need the owner to do"},
                  "task_id": {"type": "string"}},
      required=["category", "summary"])
async def request_human(ctx: Ctx, args: dict) -> ToolOutput:  # never runs: always a hard stop
    raise PolicyRefusal("hard_stop", "unreachable")


@tool("list_team", grant="core", action_class="internal",
      description="Your supervisor, direct reports and org members (ids for messages and tasks).")
async def list_team(ctx: Ctx, args: dict) -> dict:
    return team(ctx.db, ctx.agent)


@tool("list_tasks", grant="core", action_class="internal",
      description="Tasks assigned to you or to agents you created tasks for.",
      properties={"status": {"type": "string"}})
async def list_tasks(ctx: Ctx, args: dict) -> list:
    status = args.get("status")
    sql = ("SELECT id, title, status, assignee_id, created_by, deliverable, result FROM tasks "
           "WHERE (assignee_id = ? OR created_by = ?)")
    params: list = [ctx.agent["id"], ctx.agent["id"]]
    if status:
        sql += " AND status = ?"
        params.append(status)
    return ctx.db.all(sql + " ORDER BY updated_at DESC LIMIT 30", tuple(params))


@tool("yield_run", grant="core", action_class="internal",
      description="End this run and go to sleep. Summarize what you did and what you're waiting for.",
      properties={"summary": {"type": "string"}}, required=["summary"])
async def yield_run(ctx: Ctx, args: dict) -> str:
    ctx.yielded = args.get("summary") or "yielded"
    return "ok, sleeping"

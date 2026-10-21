"""Fixes for the owner's complaints from the live chat: no 'done' before the result, no lost messages,
out-of-credit handled once, and a measured answer to 'is anything happening?'."""

import pytest

from test_ideas_chat import PROPOSAL, analyst

from bizops import ideas
from bizops.tools import Ctx


async def test_owner_reply_waits_for_the_turns_actions(core, org):
    """The CEO said 'Done, launching' while the launch was being refused. Now the reply goes out after the
    action, with the refusal attached."""
    from bizops.runtime import run_turn
    order = []
    p = ideas.submit_proposal(core.db, core.settings, author=analyst(core), fields={**PROPOSAL, "title": "Unreviewed"})
    ctx = Ctx(core=core, agent=org["pceo"])
    import bizops.runtime as rt
    real = rt.invoke

    async def spy(ctx_, name, args, **kw):
        order.append(name)
        return await real(ctx_, name, args, **kw)
    rt.invoke = spy
    try:
        results = await run_turn(ctx, [
            {"id": "a", "name": "reply_human", "input": {"text": "Done. Launching it now."}},
            {"id": "b", "name": "decide_proposal", "input": {"proposal_id": p["id"], "decision": "approve", "reason": "go"}},
        ])
    finally:
        rt.invoke = real
    assert order == ["decide_proposal", "reply_human"]
    assert results[0].ok and not results[1].ok  # results stay in the model's order
    msg = core.db.one("SELECT body FROM messages WHERE recipient_kind = 'human' ORDER BY created_at DESC")
    assert "Done. Launching it now." in msg["body"] and "did not go through" in msg["body"]
    assert "decide_proposal" in msg["body"] and "red team" in msg["body"]


async def test_out_of_credit_requeues_messages_and_asks_owner_once(core, org):
    from bizops.engine import get_setting, human_message
    from bizops.llm.base import LLMOutOfCredit
    from bizops.runtime import run_agent
    msg = human_message(core.db, core.settings, org["pceo"]["id"], "the idea was us for ourselves", type="chat")

    async def broke(**kw):
        raise LLMOutOfCredit("Your credit balance is too low")
    core.llm.complete = broke
    for _ in range(2):
        await run_agent(core, org["pceo"]["id"], "message")
    assert core.db.scalar("SELECT status FROM messages WHERE id = ?", (msg["id"],)) == "unread"  # not lost
    assert core.db.scalar("SELECT wake_requested_at FROM agents WHERE id = ?", (org["pceo"]["id"],))
    assert get_setting(core.db, "llm_paused_until", "")
    items = core.db.all("SELECT * FROM policy_decisions WHERE rule = 'hard_stop.model_credits' AND status = 'open'")
    assert len(items) == 1 and "Plans & Billing" in items[0]["args_json"]


def test_credit_errors_are_recognised():
    from bizops.llm.anthropic_client import _raise_if_out_of_credit
    from bizops.llm.base import LLMOutOfCredit

    class E(Exception):
        status_code = 400
    with pytest.raises(LLMOutOfCredit):
        _raise_if_out_of_credit(E("Error code: 400 - Your credit balance is too low to access the Anthropic API"))
    _raise_if_out_of_credit(E("Error code: 400 - messages: field required"))  # other 400s pass through


def test_measured_today_status(core, org):
    from fastapi.testclient import TestClient

    from bizops import business
    from bizops.runtime import gather_context
    from bizops.status import org_today
    from bizops.web.app import create_app
    lead = business.add_lead(core.db, org_id=org["org"], agent=org["ceo"], company="Acme", email="a@acme.com",
                             source_url="https://acme.com")
    t = org_today(core.db, org["org"])
    assert t["leads_added"] == 1 and t["emails_sent"] == 0 and t["state"] in ("active today", "idle")
    ctx_text = gather_context(core, org["pceo"], "timer")[0]
    assert "Each business today (measured" in ctx_text and "1 new leads" in ctx_text
    assert "Each business today" not in gather_context(core, org["ceo"], "timer")[0]
    with TestClient(create_app(core, run_scheduler=False)) as c:
        html = c.get("/").text
    assert "Today:" in html and "1 new leads" in html
    assert lead

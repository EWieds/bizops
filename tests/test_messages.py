import pytest

from bizops.engine import (PolicyRefusal, create_org_from_template, expire_messages, human_message, post_message)
from bizops.gateway import invoke
from bizops.tools import Ctx


async def test_message_wakes_recipient(core, org):
    r = await invoke(Ctx(core=core, agent=org["coo"]), "message",
                     {"to": org["researcher"]["id"], "type": "instruction", "text": "go"})
    assert r.ok
    assert core.db.scalar("SELECT wake_requested_at FROM agents WHERE id = ?", (org["researcher"]["id"],))


def test_hop_limit(core, org):
    s = core.settings
    a, b = org["coo"], org["cfo"]
    m = post_message(core.db, s, org_id=org["org"], sender_kind="agent", sender_id=a["id"], recipient_kind="agent",
                     recipient_id=b["id"], type="question", body="q")
    for i in range(s.policy["messages"]["max_hops"]):
        snd, rcv = (b, a) if i % 2 == 0 else (a, b)
        m = post_message(core.db, s, org_id=org["org"], sender_kind="agent", sender_id=snd["id"],
                         recipient_kind="agent", recipient_id=rcv["id"], type="answer", body="a", in_reply_to=m["id"])
    with pytest.raises(PolicyRefusal) as e:
        post_message(core.db, s, org_id=org["org"], sender_kind="agent", sender_id=a["id"], recipient_kind="agent",
                     recipient_id=b["id"], type="answer", body="again", in_reply_to=m["id"])
    assert e.value.rule == "message.max_hops"


def test_ttl_expiry(core, org):
    m = human_message(core.db, core.settings, org["ceo"]["id"], "hi")
    core.db.execute("UPDATE messages SET expires_at = '2000-01-01T00:00:00Z' WHERE id = ?", (m["id"],))
    assert expire_messages(core.db) == 1
    assert core.db.scalar("SELECT status FROM messages WHERE id = ?", (m["id"],)) == "expired"


async def test_cross_org_peer_refused_supervisor_edge_allowed(core, org):
    other = create_org_from_template(core.db, core.settings, template="generic", name="Other Co",
                                     parent_org_id=org["root"], supervisor_agent_id=org["pceo"]["id"])
    other_coo = core.db.one("SELECT * FROM agents WHERE org_id = ? AND role = 'coo'", (other,))
    r = await invoke(Ctx(core=core, agent=org["coo"]), "message",
                     {"to": other_coo["id"], "type": "question", "text": "hi"})
    assert not r.ok and "message.scope" in r.content
    r = await invoke(Ctx(core=core, agent=org["ceo"]), "recommend_up", {"text": "advice"})
    assert r.ok  # org CEO -> platform CEO is a supervisor edge
    assert core.db.scalar("SELECT type FROM messages WHERE recipient_id = ?", (org["pceo"]["id"],)) == "recommendation"


async def test_task_scope(core, org):
    r = await invoke(Ctx(core=core, agent=org["writer"]), "create_task",
                     {"assignee": org["ceo"]["id"], "title": "x", "deliverable": "y"})
    assert not r.ok and "task.scope" in r.content
    r = await invoke(Ctx(core=core, agent=org["ceo"]), "create_task",
                     {"assignee": org["writer"]["id"], "title": "x", "deliverable": "y"})
    assert r.ok  # writer is a descendant of the CEO


async def test_reply_human_ceo_only(core, org):
    assert not (await invoke(Ctx(core=core, agent=org["coo"]), "reply_human", {"text": "hi"})).ok
    assert (await invoke(Ctx(core=core, agent=org["ceo"]), "reply_human", {"text": "hi"})).ok

import io

import pytest
from fastapi.testclient import TestClient

from conftest import with_csrf
from bizops import ideas, media
from bizops.engine import org_ceo
from bizops.llm.dry_run import DEMO_CASE
from bizops.gateway import invoke
from bizops.tools import Ctx
from bizops.web.app import create_app

PROPOSAL = {"title": "Review Reply Service", "summary": "We answer Google reviews for local businesses.",
            "thesis": "Owners don't have time to reply to reviews", "target_customer": "Local restaurants",
            "offer": "$99/month", "evidence": [{"claim": "Replies improve ratings", "source_url": "https://example.com/study"}],
            "plan": "Week 1 build; weeks 2-4 outreach", "kill_criteria": "No customer by day 45",
            "scores": {k: 4 for k in ideas.SCORE_WEIGHTS}, "template": "generic", "business_case": DEMO_CASE}


def red_team(core, pid, verdict="go", **over):
    """A complete, valid red-team review (tests that need one but aren't about the review itself)."""
    rt = core.db.one("SELECT * FROM agents WHERE role = 'red_team'")
    review = {"verdict": verdict, "summary": "Paid demand is real but replies will be lower than planned.",
              "fatal_flaws": [] if verdict != "no_go" else ["no proof anyone pays"],
              "required_changes": ["cut the reply-rate assumption to 2%"] if verdict == "fix" else [],
              "risks": [{"risk": f"risk {i}", "likelihood": "medium", "impact": "high", "mitigation": "m"} for i in range(5)],
              "counter_evidence": [{"claim": "cheaper rivals", "source_url": "https://example.com/a"},
                                   {"claim": "complaints", "source_url": "https://example.com/b"}],
              "assumption_challenges": [{"assumption": f"a{i}", "challenge": "too rosy", "revised_value": "lower"} for i in range(3)],
              "revised_scores": {k: 4 for k in ideas.SCORE_WEIGHTS},
              "revised_projection": {"time_to_first_revenue_days": 30, "month3_profit_usd": 100, "month6_profit_usd": 600},
              "kill_criteria": "No paying customer by day 45.", **over}
    return ideas.record_red_team(core.db, core.settings, reviewer=rt, proposal_id=pid, review=review)


def analyst(core):
    return core.db.one("SELECT * FROM agents WHERE role = 'venture_analyst'")


async def test_submit_scores_and_notifies_ceo(core, org):
    r = await invoke(Ctx(core=core, agent=analyst(core)), "submit_proposal", PROPOSAL)
    assert r.ok, r.content
    p = core.db.one("SELECT * FROM proposals")
    assert p["total_score"] == 4.0 and p["status"] == "red_team"
    assert core.db.one("SELECT 1 AS x FROM tasks t JOIN agents a ON a.id = t.assignee_id WHERE a.role = 'red_team' "
                       "AND t.title LIKE ?", (f"Red-team {p['id']}%",))
    assert not core.db.one("SELECT 1 AS x FROM messages WHERE recipient_id = ? AND body LIKE 'Reviewed proposal%'",
                           (org["pceo"]["id"],))  # nothing to decide before the red team has spoken
    red_team(core, p["id"])
    msg = core.db.one("SELECT body FROM messages WHERE recipient_id = ? AND body LIKE 'Reviewed proposal%'", (org["pceo"]["id"],))
    assert "Standing authorization" in msg["body"] and "verdict go" in msg["body"]
    bad = {**PROPOSAL, "evidence": []}
    r = await invoke(Ctx(core=core, agent=analyst(core)), "submit_proposal", bad)
    assert not r.ok and "evidence" in r.content


async def test_ceo_launches_within_venture_limits(core, org):
    core.settings.policy["platform"]["monthly_cap_usd"] = 1000
    c = Ctx(core=core, agent=org["pceo"])
    ids = []
    for i in range(3):
        ids.append(ideas.submit_proposal(core.db, core.settings, author=analyst(core), fields={**PROPOSAL, "title": f"Idea {i}"})["id"])
        red_team(core, ids[-1])
    for pid in ids[:2]:
        r = await invoke(c, "decide_proposal", {"proposal_id": pid, "decision": "approve", "reason": "strong"})
        assert r.ok, r.content
    r = await invoke(c, "decide_proposal", {"proposal_id": ids[2], "decision": "approve", "reason": "strong"})
    assert not r.ok and "venture" in r.content
    org_id = core.db.scalar("SELECT org_id FROM proposals WHERE id = ?", (ids[0],))
    o = core.db.one("SELECT * FROM orgs WHERE id = ?", (org_id,))
    assert o["status"] == "trial" and "Owners don't have time" in o["brief"]
    assert core.db.scalar("SELECT decision_reason FROM proposals WHERE id = ?", (ids[0],)) == "strong"


def test_research_cycle_tasks_the_analyst_once(core, org):
    core.settings.policy["venture"]["research_every_days"] = 3
    assert ideas.research_cycle(core.db, core.settings) is True
    assert ideas.research_cycle(core.db, core.settings) is False
    t = core.db.one("SELECT * FROM tasks WHERE title LIKE 'Research cycle%'")
    assert t["assignee_id"] == analyst(core)["id"]


async def test_dry_run_research_to_launch(core, org):
    """Research cycle -> proposal -> platform CEO auto-launch, end to end on the scripted model."""
    from bizops.scheduler import Scheduler
    core.settings.policy["venture"]["research_every_days"] = 3
    core.settings.policy["platform"]["monthly_cap_usd"] = 1000
    ideas.research_cycle(core.db, core.settings)
    core.db.execute("UPDATE agents SET min_interval_s = 0")
    await Scheduler(core, tick_s=0.05).drain(timeout=60)
    p = core.db.one("SELECT * FROM proposals")
    assert p and p["status"] == "launched" and p["org_id"]
    assert p["redteam_verdict"] == "go" and p["redteam_by"] != p["created_by"]


def test_extract_text_pdf_and_docx():
    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    assert media.extract_text(buf.getvalue(), "x.pdf") == ""
    assert media.extract_text(b"hello,world", "a.csv") == "hello,world"
    assert media.youtube_ids("see https://youtu.be/dQw4w9WgXcQ and https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3") == ["dQw4w9WgXcQ"]
    assert media.youtube_ids("https://www.youtube.com/shorts/abcdefghijk") == ["abcdefghijk"]


async def test_youtube_tool(core, org, monkeypatch):
    monkeypatch.setattr(media, "_transcript_sync", lambda vid, langs: [{"text": "Start a lawn care business", "start": 0.0},
                                                                         {"text": "charge per visit", "start": 75.0}])
    a = analyst(core)
    r = await invoke(Ctx(core=core, agent=a), "youtube_transcript", {"url": "https://youtu.be/dQw4w9WgXcQ"})
    assert r.ok and "lawn care" in r.content and "untrusted" in r.content
    assert core.db.one("SELECT * FROM artifacts WHERE name = 'youtube-dQw4w9WgXcQ.md'")
    r = await invoke(Ctx(core=core, agent=a), "youtube_transcript", {"url": "https://example.com"})
    assert not r.ok


def test_chat_upload_youtube_and_quick_action(core, org, monkeypatch):
    monkeypatch.setattr(media, "_transcript_sync", lambda vid, langs: [{"text": "How I built an agency", "start": 0.0}])
    pceo = org["pceo"]
    with TestClient(create_app(core, run_scheduler=False)) as c:
        with_csrf(c)
        r = c.post("/chat", data={"agent": pceo["id"], "text": "Look at https://youtu.be/dQw4w9WgXcQ", "action": "plan"},
                   files=[("files", ("notes.txt", b"Idea: sell review replies to restaurants", "text/plain"))],
                   follow_redirects=False)
        assert r.status_code == 303
        msg = core.db.one("SELECT * FROM messages WHERE recipient_id = ? AND sender_kind = 'human' ORDER BY created_at DESC",
                          (pceo["id"],))
        assert "business plan" in msg["body"] and "artifact" in msg["body"] and "How I built an agency" in msg["body"]
        assert "notes.txt" in msg["attachments_json"] and "youtube" in msg["attachments_json"]
        page = c.get(f"/chat/thread?agent={pceo['id']}").text
        assert "notes.txt" in page
        assert c.get("/ideas").status_code == 200


def test_owner_launch_from_ideas_page(core, org):
    core.settings.policy["platform"]["monthly_cap_usd"] = 1000
    p = ideas.submit_proposal(core.db, core.settings, author=None, fields=PROPOSAL)
    with TestClient(create_app(core, run_scheduler=False)) as c:
        with_csrf(c)
        assert c.post(f"/ideas/{p['id']}/launch", follow_redirects=False).status_code == 400  # red team first
        red_team(core, p["id"], verdict="no_go")
        assert "red team: no-go" in c.get("/ideas").text
        r = c.post(f"/ideas/{p['id']}/launch", follow_redirects=False)  # the owner may overrule it
        assert r.status_code == 303 and r.headers["location"].startswith("/orgs/")
        assert core.db.scalar("SELECT status FROM proposals WHERE id = ?", (p["id"],)) == "launched"
        assert "owner override" in core.db.scalar("SELECT decision_reason FROM proposals WHERE id = ?", (p["id"],))


def test_duplicate_proposals_refused(core, org):
    from bizops.engine import PolicyRefusal
    ideas.submit_proposal(core.db, core.settings, author=None, fields=PROPOSAL)
    with pytest.raises(PolicyRefusal) as e:
        ideas.submit_proposal(core.db, core.settings, author=None, fields={**PROPOSAL, "title": "review reply service!"})
    assert e.value.rule == "proposal.duplicate"

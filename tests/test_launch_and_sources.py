"""The dynamic launch bar and YouTube channels as an idea source."""

import pytest
from fastapi.testclient import TestClient

from conftest import with_csrf

from bizops import idea_sources, media
from bizops.ideas import SCORE_WEIGHTS, launch_bar


def review(m6=3000, **scores):
    s = {k: 3 for k in SCORE_WEIGHTS} | scores
    return {"revised_scores": s, "revised_projection": {"month6_profit_usd": m6}}


def test_cheap_fast_upside_lowers_the_bar_and_risk_raises_it(settings):
    plain = launch_bar(settings, {"total_score": 3.0}, review())
    cheap = launch_bar(settings, {"total_score": 3.0}, review(m6=20000, cost_to_test=5, time_to_revenue=5))
    risky = launch_bar(settings, {"total_score": 4.6}, review(legal_risk=2.5, owner_steps=2, cost_to_test=1))
    assert cheap["bar"] < plain["bar"] < risky["bar"]
    assert any("cheap/fast" in w for w in cheap["why"]) and any("legal" in w for w in risky["why"])
    assert any("author" in w for w in risky["why"])
    assert 2.5 <= cheap["bar"] and risky["bar"] <= 3.8


def test_floors_and_profit_block_even_a_high_score(settings):
    lb = launch_bar(settings, {"total_score": 4}, review(legal_risk=2, automation_fit=5, time_to_revenue=5, cost_to_test=5))
    assert lb["floors_failed"] and not lb["clears"]
    settings.policy["venture"]["launch_rule"] = {"require_month6_profit": True}
    assert not launch_bar(settings, {"total_score": 4}, review(m6=0, demand=5, automation_fit=5))["clears"]
    settings.policy["venture"]["launch_rule"] = {}
    good = launch_bar(settings, {"total_score": 3.6}, review(m6=12000, demand=4, automation_fit=4, cost_to_test=5,
                                                             time_to_revenue=4))
    assert good["clears"] and good["score"] >= good["bar"]


def test_rule_is_tunable_in_policy(settings):
    settings.policy["venture"]["launch_rule"] = {"base": 2.0, "min": 1.0}
    assert launch_bar(settings, {"total_score": 3}, review(m6=1000, legal_risk=4))["bar"] == 2.0


VIDEOS = [  # the channel's own order: newest first
    {"video_id": "AAAAAAAAAAA", "title": "Newest, few views", "view_count": 900, "duration_s": 900},
    {"video_id": "BBBBBBBBBBB", "title": "Big hit", "view_count": 133000, "duration_s": 2600},
    {"video_id": "CCCCCCCCCCC", "title": "A short", "view_count": 500000, "duration_s": 40},
    {"video_id": "DDDDDDDDDDD", "title": "Solid", "view_count": 62000, "duration_s": 2600},
    {"video_id": "EEEEEEEEEEE", "title": "Views unknown", "view_count": None, "duration_s": None},
]


@pytest.fixture
def fake_youtube(monkeypatch):
    state = {"videos": list(VIDEOS), "transcribed": []}

    async def get(url, params=None):
        return '<html><meta itemprop="identifier" content="UC1234567890123456789012"></html>'

    async def listing(core, channel_id):
        return "Side Hustle Lab", list(state["videos"]), "test"

    async def yt(core, vid):
        state["transcribed"].append(vid)
        return {"video_id": vid, "url": f"https://www.youtube.com/watch?v={vid}", "title": f"Video {vid[:1]}",
                "transcript": "how I started a lawn care lead business " * 80, "minutes": 12}

    monkeypatch.setattr(idea_sources, "_http_get", get)
    monkeypatch.setattr(idea_sources, "list_videos", listing)
    monkeypatch.setattr(media, "youtube", yt)
    return state


def analyst_tasks(core):
    return core.db.all("SELECT t.* FROM tasks t JOIN agents a ON a.id = t.assignee_id WHERE a.role = 'venture_analyst' "
                       "AND t.title LIKE 'Ideas from YouTube%' ORDER BY t.created_at")


async def test_whole_channel_most_viewed_first_new_uploads_jump_the_queue(core, org, fake_youtube):
    core.settings.policy["venture"]["youtube"] = {"max_videos_per_check": 2}
    src = await idea_sources.add_channel(core, "https://www.youtube.com/@sidehustlelab")
    assert src["channel_id"] == "UC1234567890123456789012" and src["title"] == "Side Hustle Lab"
    assert src["total_videos"] == 5
    assert await idea_sources.poll(core) == 2
    assert fake_youtube["transcribed"] == ["BBBBBBBBBBB", "DDDDDDDDDDD"]  # most viewed; the 40s short is skipped
    assert "133,000 views" in analyst_tasks(core)[0]["description"]
    assert await idea_sources.poll(core) == 0  # throttled
    fake_youtube["videos"].insert(0, {"video_id": "FFFFFFFFFFF", "title": "Brand new", "view_count": 10, "duration_s": 600})
    assert await idea_sources.poll(core, force=True) == 2
    assert fake_youtube["transcribed"][2:] == ["FFFFFFFFFFF", "AAAAAAAAAAA"]  # the new upload first, then the backlog
    assert await idea_sources.poll(core, force=True) == 1  # finally the one without a view count
    assert await idea_sources.poll(core, force=True) == 0  # the whole channel is done
    again = await idea_sources.add_channel(core, "@sidehustlelab", order_by="newest")
    assert again["id"] == src["id"] and again["order_by"] == "newest"


async def test_newest_first_and_top_n(core, org, fake_youtube):
    core.settings.policy["venture"]["youtube"] = {"max_videos_per_check": 10}
    await idea_sources.add_channel(core, "@sidehustlelab", order_by="newest", max_videos=2)
    assert await idea_sources.poll(core) == 2
    assert fake_youtube["transcribed"] == ["AAAAAAAAAAA", "BBBBBBBBBBB"]
    assert await idea_sources.poll(core, force=True) == 0  # top 2 only


def test_youtube_api_durations():
    assert idea_sources._iso_duration("PT44M51S") == 2691 and idea_sources._iso_duration("PT1H2M") == 3720
    assert idea_sources._iso_duration("P1DT1S") == 86401


async def test_bad_channel_input_is_refused(core, org):
    with pytest.raises(Exception, match="youtube.channel"):
        await idea_sources.add_channel(core, "https://example.com/not-youtube")


def test_ideas_page_adds_and_lists_channels(core, org, fake_youtube):
    from bizops.web.app import create_app
    with TestClient(create_app(core, run_scheduler=False)) as c:
        with_csrf(c)
        r = c.post("/ideas/sources", data={"url": "@sidehustlelab", "order_by": "views", "max_videos": "25"},
                   follow_redirects=False)
        assert r.status_code == 303 and "source_added" in r.headers["location"]
        page = c.get("/ideas").text
        assert "Side Hustle Lab" in page and "most viewed first" in page and "top 25" in page


def test_cheap_ideas_launch_whatever_the_score(settings):
    import json
    cheap = {"total_score": 2, "business_case_json": json.dumps({"costs": {"startup_usd": 200, "monthly_usd": 250}})}
    pricey = {"total_score": 2, "business_case_json": json.dumps({"costs": {"startup_usd": 2000, "monthly_usd": 250}})}
    bad = review(**{**{k: 1.5 for k in SCORE_WEIGHTS}, "legal_risk": 3})
    assert launch_bar(settings, cheap, bad)["clears"] and launch_bar(settings, cheap, bad)["cheap"]
    assert not launch_bar(settings, pricey, bad)["clears"]
    assert not launch_bar(settings, cheap, review(legal_risk=2))["clears"]  # legal risk still blocks


def test_venture_fund_seeds_each_launch(core, org):
    import json
    from bizops import ideas, ledger
    from bizops.payments import deposit
    deposit(core.db, org_id=org["root"], amount_usd=700)
    p = {"id": "prop_x", "title": "Thing", "business_case_json": json.dumps({"costs": {"startup_usd": 100, "monthly_usd": 300}})}
    assert ideas.fund_launch(core.db, core.settings, org["root"], org["org"], p) == 400_000_000
    assert ledger.wallet_balance(core.db, org["org"]) == 400_000_000
    assert ledger.wallet_balance(core.db, org["root"]) == 300_000_000
    assert ideas.fund_launch(core.db, core.settings, org["root"], org["org"], p) == 300_000_000  # what's left
    assert ideas.fund_launch(core.db, core.settings, org["root"], org["org"], p) == 0


def test_sweep_launches_qualifying_and_revives_red_team_parked(core, org):
    from test_ideas_chat import PROPOSAL, analyst, red_team
    from bizops import ideas
    core.settings.policy["venture"]["launch_in_code"] = True
    p = ideas.submit_proposal(core.db, core.settings, author=analyst(core), fields=dict(PROPOSAL))  # $65 to start
    core.settings.policy["venture"]["launch_in_code"] = False
    red_team(core, p["id"], verdict="no_go")
    core.db.execute("UPDATE proposals SET status = 'parked' WHERE id = ?", (p["id"],))  # parked under the old rules
    owner_parked = ideas.submit_proposal(core.db, core.settings, author=analyst(core), fields={**PROPOSAL, "title": "Mine"})
    red_team(core, owner_parked["id"])
    core.db.execute("UPDATE proposals SET status = 'parked', decided_by = 'human' WHERE id = ?", (owner_parked["id"],))
    core.settings.policy["venture"]["launch_in_code"] = True
    assert ideas.launch_sweep(core.db, core.settings) == 1
    assert core.db.scalar("SELECT status FROM proposals WHERE id = ?", (p["id"],)) == "launched"
    assert core.db.scalar("SELECT status FROM proposals WHERE id = ?", (owner_parked["id"],)) == "parked"


def test_low_org_cap_doesnt_block_a_launch_without_caps(core, org):
    from bizops.engine import create_org_from_template
    core.settings.policy["budgets"].update({"spend_caps": False, "agent_budgets": False})
    oid = create_org_from_template(core.db, core.settings, template="generic", name="Tiny", parent_org_id=org["root"],
                                   supervisor_agent_id=org["pceo"]["id"], monthly_cap_usd=5)
    assert core.db.scalar("SELECT COUNT(*) FROM agents WHERE org_id = ?", (oid,)) > 0

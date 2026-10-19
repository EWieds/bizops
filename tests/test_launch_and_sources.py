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
    assert 2.8 <= cheap["bar"] and risky["bar"] <= 4.2


def test_floors_and_profit_block_even_a_high_score(settings):
    lb = launch_bar(settings, {"total_score": 4}, review(demand=2, automation_fit=5, time_to_revenue=5, cost_to_test=5))
    assert lb["floors_failed"] and not lb["clears"]
    assert not launch_bar(settings, {"total_score": 4}, review(m6=0, demand=5, automation_fit=5))["clears"]
    good = launch_bar(settings, {"total_score": 3.6}, review(m6=12000, demand=4, automation_fit=4, cost_to_test=5,
                                                             time_to_revenue=4))
    assert good["clears"] and good["score"] >= good["bar"]


def test_rule_is_tunable_in_policy(settings):
    settings.policy["venture"]["launch_rule"] = {"base": 2.0, "min": 1.0}
    assert launch_bar(settings, {"total_score": 3}, review(m6=1000, legal_risk=4))["bar"] == 2.0


FEED_XML = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
<title>Side Hustle Lab</title>
<entry><yt:videoId>{a}</yt:videoId><title>Video A</title><published>2026-10-18T10:00:00+00:00</published></entry>
<entry><yt:videoId>BBBBBBBBBBB</yt:videoId><title>Video B</title><published>2026-10-10T10:00:00+00:00</published></entry>
<entry><yt:videoId>CCCCCCCCCCC</yt:videoId><title>Video C</title><published>2026-10-01T10:00:00+00:00</published></entry>
</feed>"""


@pytest.fixture
def fake_youtube(monkeypatch):
    state = {"latest": "AAAAAAAAAAA"}

    async def get(url):
        if "feeds/videos.xml" in url:
            return FEED_XML.format(a=state["latest"])
        return '<html><meta itemprop="identifier" content="UC1234567890123456789012"></html>'

    async def yt(core, vid):
        return {"video_id": vid, "url": f"https://www.youtube.com/watch?v={vid}", "title": f"Video {vid[:1]}",
                "transcript": "how I started a lawn care lead business " * 80, "minutes": 12}

    monkeypatch.setattr(idea_sources, "_http_get", get)
    monkeypatch.setattr(media, "youtube", yt)
    return state


async def test_channel_videos_become_analyst_tasks(core, org, fake_youtube):
    core.settings.policy["venture"]["youtube"] = {"backfill_videos": 2}
    src = await idea_sources.add_channel(core, "https://www.youtube.com/@sidehustlelab")
    assert src["channel_id"] == "UC1234567890123456789012" and src["title"] == "Side Hustle Lab"
    assert await idea_sources.poll(core) == 2  # the latest two; the older one is skipped
    tasks = core.db.all("SELECT t.* FROM tasks t JOIN agents a ON a.id = t.assignee_id WHERE a.role = 'venture_analyst' "
                        "AND t.title LIKE 'Ideas from YouTube%'")
    assert len(tasks) == 2 and "read_artifact art_" in tasks[0]["description"]
    assert await idea_sources.poll(core) == 0  # throttled
    fake_youtube["latest"] = "DDDDDDDDDDD"  # a new upload
    assert await idea_sources.poll(core, force=True) == 1
    again = await idea_sources.add_channel(core, "@sidehustlelab")
    assert again["id"] == src["id"]


async def test_bad_channel_input_is_refused(core, org):
    with pytest.raises(Exception, match="youtube.channel"):
        await idea_sources.add_channel(core, "https://example.com/not-youtube")


def test_ideas_page_adds_and_lists_channels(core, org, fake_youtube):
    from bizops.web.app import create_app
    with TestClient(create_app(core, run_scheduler=False)) as c:
        with_csrf(c)
        r = c.post("/ideas/sources", data={"url": "@sidehustlelab"}, follow_redirects=False)
        assert r.status_code == 303 and "source_added" in r.headers["location"]
        page = c.get("/ideas").text
        assert "Side Hustle Lab" in page and "YouTube channels" in page

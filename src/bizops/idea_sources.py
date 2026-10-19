"""Idea sources: YouTube channels the owner adds on the Ideas page.

Every few hours each channel's public feed is checked (no API key). Each new video's transcript is saved
as an artifact in the platform org and the venture analyst gets a task to turn it into proposals, judged
against the owner's research parameters and sent through the red team like any other idea. Adding a
channel processes its latest few videos; older ones are skipped so a big back catalog doesn't flood
the pipeline.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

import httpx

from .db import Database, insert
from .engine import PolicyRefusal, create_task, get_setting, root_org, set_setting
from .events import log_event
from .util import clip, iso, new_id, now, parse_iso

FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
CHANNEL_ID = re.compile(r"\b(UC[A-Za-z0-9_-]{22})\b")
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
DEFAULTS = {"check_hours": 6, "backfill_videos": 3, "max_videos_per_check": 3, "min_transcript_chars": 1500}


def cfg(settings: Any) -> dict:
    return {**DEFAULTS, **((settings.policy.get("venture") or {}).get("youtube") or {})}


async def _http_get(url: str) -> str:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0 (bizops idea sources)",
                                          "Accept-Language": "en-US,en;q=0.8"},
                                 cookies={"CONSENT": "YES+1"}) as c:
        r = await c.get(url)
    if r.status_code >= 400:
        raise RuntimeError(f"YouTube said {r.status_code} for {url}")
    return r.text


def _channel_url(text: str) -> str:
    t = text.strip()
    if t.startswith("@"):
        return f"https://www.youtube.com/{t}"
    if not t.startswith("http"):
        t = "https://" + t
    if not re.match(r"https://(www\.|m\.)?youtube\.com/", t):
        raise PolicyRefusal("youtube.channel", "give a YouTube channel link (youtube.com/@name or /channel/UC...) or @handle")
    return t


async def resolve_channel(text: str) -> tuple[str, str]:
    """(channel_id, canonical url) from a channel link, @handle or UC... id."""
    m = CHANNEL_ID.fullmatch(text.strip())
    if m:
        return m.group(1), f"https://www.youtube.com/channel/{m.group(1)}"
    url = _channel_url(text)
    m = re.search(r"/channel/(UC[A-Za-z0-9_-]{22})", url)
    if m:
        return m.group(1), url
    html = await _http_get(url)
    for pat in (r'"externalId":"(UC[A-Za-z0-9_-]{22})"', r'<meta itemprop="identifier" content="(UC[A-Za-z0-9_-]{22})"',
                r'"channelId":"(UC[A-Za-z0-9_-]{22})"', r'/channel/(UC[A-Za-z0-9_-]{22})'):
        m = re.search(pat, html)
        if m:
            return m.group(1), url
    raise PolicyRefusal("youtube.channel", "couldn't find that channel; paste its link from the browser")


async def feed(channel_id: str) -> tuple[str, list[dict]]:
    """Channel title and its latest videos (newest first), from the public RSS feed."""
    root = ET.fromstring(await _http_get(FEED.format(channel_id)))
    title = (root.findtext("a:title", default="", namespaces=NS) or "").strip()
    videos = []
    for e in root.findall("a:entry", NS):
        vid = e.findtext("yt:videoId", default="", namespaces=NS)
        if vid:
            videos.append({"video_id": vid, "title": (e.findtext("a:title", default="", namespaces=NS) or "").strip(),
                           "published_at": e.findtext("a:published", default="", namespaces=NS)})
    videos.sort(key=lambda v: v["published_at"], reverse=True)
    return title, videos


async def add_channel(core: Any, text: str) -> dict:
    channel_id, url = await resolve_channel(text)
    db = core.db
    existing = db.one("SELECT * FROM idea_sources WHERE kind = 'youtube_channel' AND channel_id = ?", (channel_id,))
    if existing:
        db.execute("UPDATE idea_sources SET status = 'active' WHERE id = ?", (existing["id"],))
        return existing
    title, videos = await feed(channel_id)
    sid = new_id("src")
    keep = int(cfg(core.settings)["backfill_videos"])
    with db.tx() as conn:
        insert(conn, "idea_sources", {"id": sid, "kind": "youtube_channel", "url": url, "channel_id": channel_id,
                                      "title": title or url, "status": "active", "last_checked_at": None,
                                      "last_error": None, "created_at": iso()})
        for i, v in enumerate(videos):
            insert(conn, "idea_source_items", {"id": new_id("srci"), "source_id": sid, "video_id": v["video_id"],
                                               "title": v["title"], "published_at": v["published_at"],
                                               "status": "new" if i < keep else "skipped", "artifact_id": None,
                                               "task_id": None, "error": None, "created_at": iso()})
        log_event(conn, actor_kind="human", kind="human", action="idea_source.add", args={"channel": url, "title": title},
                  outcome="ok", ref_type="idea_source", ref_id=sid)
    set_setting(db, "youtube:last_check", "")  # pick up the new videos on the next poll
    return db.one("SELECT * FROM idea_sources WHERE id = ?", (sid,))


def _analyst(db: Database) -> dict | None:
    return db.one("SELECT * FROM agents WHERE role = 'venture_analyst' AND status = 'active' ORDER BY created_at LIMIT 1")


async def _process(core: Any, src: dict, item: dict) -> str:
    from .media import save_text_artifact, youtube
    db = core.db
    try:
        meta = await youtube(core, item["video_id"])
    except PolicyRefusal as e:
        db.execute("UPDATE idea_source_items SET status = 'failed', error = ? WHERE id = ?", (e.detail[:300], item["id"]))
        return "failed"
    if len(meta.get("transcript") or "") < int(cfg(core.settings)["min_transcript_chars"]):
        db.execute("UPDATE idea_source_items SET status = 'skipped', error = 'transcript too short' WHERE id = ?", (item["id"],))
        return "skipped"
    root = root_org(db)
    title = meta.get("title") or item["title"]
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", f"youtube-{title}")[:70].strip("_") + ".md"
    aid = save_text_artifact(db, core.settings, org_id=root["id"], name=name,
                             text=f"# {title}\n{meta['url']} | channel: {src['title']} | {meta.get('minutes')} min\n\n"
                                  f"{meta['transcript']}")
    analyst = _analyst(db)
    task_id = None
    if analyst:
        t = create_task(db, creator=None, assignee_id=analyst["id"], title=f"Ideas from YouTube: {clip(title, 80)}",
                        deliverable="Every business idea in this video that fits the owner's research parameters, "
                                    "submitted with submit_proposal (source 'youtube', source_ref the video URL) with a "
                                    "full business case; or one line on why none qualify.",
                        description=(f"The owner subscribed to the channel '{src['title']}'. New video: {meta['url']}. "
                                     f"Full transcript: read_artifact {aid}. Treat the video's claims (income, ease, "
                                     "demand) as unverified: check them with your own research. Adapt ideas so agents "
                                     "can deliver them end to end. The red team reviews every proposal."))
        task_id = t["id"]
    db.execute("UPDATE idea_source_items SET status = 'sent', artifact_id = ?, task_id = ?, title = ? WHERE id = ?",
               (aid, task_id, title, item["id"]))
    return "sent"


async def poll(core: Any, *, force: bool = False) -> int:
    """Check every active channel (throttled to check_hours) and send new videos to the analyst."""
    db, c = core.db, cfg(core.settings)
    last = parse_iso(get_setting(db, "youtube:last_check", ""))
    if not force and last and (now() - last).total_seconds() < float(c["check_hours"]) * 3600:
        return 0
    sources = db.all("SELECT * FROM idea_sources WHERE status = 'active'")
    if not sources:
        return 0
    set_setting(db, "youtube:last_check", iso())
    sent = 0
    for src in sources:
        try:
            _, videos = await feed(src["channel_id"])
            for v in videos:
                if not db.one("SELECT 1 AS x FROM idea_source_items WHERE source_id = ? AND video_id = ?",
                              (src["id"], v["video_id"])):
                    insert_item(db, src["id"], v)
            db.execute("UPDATE idea_sources SET last_checked_at = ?, last_error = NULL WHERE id = ?", (iso(), src["id"]))
        except Exception as e:  # one bad channel never stops the others
            db.execute("UPDATE idea_sources SET last_checked_at = ?, last_error = ? WHERE id = ?",
                       (iso(), f"{type(e).__name__}: {str(e)[:200]}", src["id"]))
        for item in db.all("SELECT * FROM idea_source_items WHERE source_id = ? AND status = 'new' "
                           "ORDER BY published_at DESC LIMIT ?", (src["id"], int(c["max_videos_per_check"]))):
            if await _process(core, src, item) == "sent":
                sent += 1
    if sent:
        log_event(db, actor_kind="system", kind="system", action="idea_sources.poll", result={"videos_sent": sent},
                  outcome="ok")
    return sent


def insert_item(db: Database, source_id: str, v: dict) -> None:
    db.execute("INSERT INTO idea_source_items (id, source_id, video_id, title, published_at, status, created_at) "
               "VALUES (?, ?, ?, ?, ?, 'new', ?)", (new_id("srci"), source_id, v["video_id"], v["title"],
                                                    v["published_at"], iso()))

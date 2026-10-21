"""Idea sources: YouTube channels the owner adds on the Ideas page.

A channel is researched in full: its whole video list is fetched with view counts, and videos are sent to
the venture analyst a few at a time, most viewed first (or newest first, per channel), optionally only
the top N. New uploads jump the queue. Each video's transcript is saved as an artifact in the platform org
and the analyst gets a task to turn it into proposals, which go through the red team like any other idea.

Listing: with YOUTUBE_API_KEY (free, Connect page) the official YouTube Data API returns every video with
views and duration. Without it, yt-dlp reads the channel's video tab, paced; YouTube cuts that off after a
few pages from cloud servers, so it covers roughly the newest 150-200 videos and only some view counts.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

from .db import Database, insert
from .engine import PolicyRefusal, create_task, get_setting, root_org, set_setting
from .events import log_event
from .util import clip, iso, new_id, now, parse_iso

CHANNEL_ID = re.compile(r"\b(UC[A-Za-z0-9_-]{22})\b")
API = "https://www.googleapis.com/youtube/v3"
DEFAULTS = {"check_hours": 3, "max_videos_per_check": 4, "min_transcript_chars": 1500, "min_duration_s": 120,
            "list_every_hours": 24}
ORDERS = {"views": "most viewed first", "newest": "newest first"}


def cfg(settings: Any) -> dict:
    return {**DEFAULTS, **((settings.policy.get("venture") or {}).get("youtube") or {})}


async def _http_get(url: str, params: dict | None = None) -> str:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0 (bizops idea sources)",
                                          "Accept-Language": "en-US,en;q=0.8"},
                                 cookies={"CONSENT": "YES+1"}) as c:
        r = await c.get(url, params=params)
    if r.status_code >= 400:
        raise RuntimeError(f"YouTube said {r.status_code} for {url.split('?')[0]}")
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


def _iso_duration(d: str) -> int:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", d or "")
    if not m:
        return 0
    days, h, mi, s = (int(x or 0) for x in m.groups())
    return days * 86400 + h * 3600 + mi * 60 + s


async def _list_api(key: str, channel_id: str) -> tuple[str, list[dict]]:
    import json
    ch = json.loads(await _http_get(f"{API}/channels", {"part": "snippet,contentDetails", "id": channel_id, "key": key}))
    items = ch.get("items") or []
    if not items:
        raise PolicyRefusal("youtube.channel", "YouTube has no channel with that id")
    title = items[0]["snippet"]["title"]
    uploads = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    videos, token = [], None
    while True:
        page = json.loads(await _http_get(f"{API}/playlistItems", {"part": "contentDetails,snippet", "maxResults": 50,
                                                                  "playlistId": uploads, "key": key,
                                                                  **({"pageToken": token} if token else {})}))
        batch = [{"video_id": it["contentDetails"]["videoId"], "title": it["snippet"]["title"],
                  "published_at": it["contentDetails"].get("videoPublishedAt") or it["snippet"].get("publishedAt")}
                 for it in page.get("items") or []]
        if batch:
            stats = json.loads(await _http_get(f"{API}/videos", {"part": "statistics,contentDetails", "key": key,
                                                                "id": ",".join(v["video_id"] for v in batch)}))
            by_id = {s["id"]: s for s in stats.get("items") or []}
            for v in batch:
                s = by_id.get(v["video_id"]) or {}
                v["view_count"] = int((s.get("statistics") or {}).get("viewCount") or 0) or None
                v["duration_s"] = _iso_duration((s.get("contentDetails") or {}).get("duration", ""))
            videos += batch
        token = page.get("nextPageToken")
        if not token or len(videos) >= 5000:
            break
    return title, videos


def _list_ytdlp_sync(channel_id: str) -> tuple[str, list[dict]]:
    import yt_dlp
    opts = {"extract_flat": "in_playlist", "quiet": True, "no_warnings": True, "skip_download": True,
            "ignoreerrors": True, "sleep_interval_requests": 2}
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(f"https://www.youtube.com/channel/{channel_id}/videos", download=False) or {}
    videos = []
    for e in info.get("entries") or []:
        if e and e.get("id"):
            videos.append({"video_id": e["id"], "title": e.get("title") or "", "view_count": e.get("view_count"),
                           "duration_s": int(e["duration"]) if e.get("duration") else None,
                           "published_at": None})
    return info.get("channel") or info.get("title") or "", videos


async def list_videos(core: Any, channel_id: str) -> tuple[str, list[dict], str]:
    """(channel title, videos in the channel's own order, newest first, listing method)."""
    key = core.settings.secret("YOUTUBE_API_KEY")
    if key:
        title, videos = await _list_api(key, channel_id)
        return title, videos, "YouTube Data API"
    title, videos = await asyncio.to_thread(_list_ytdlp_sync, channel_id)
    if not videos:
        raise RuntimeError("YouTube returned no videos for this channel (try again later, or add a YouTube API key "
                           "on the Connect page)")
    return title, videos, "yt-dlp (newest ~150-200 videos; add a YouTube API key for the full list)"


def _upsert_items(db: Database, source_id: str, videos: list[dict], *, mark_new: bool, min_duration_s: int = 0) -> int:
    added = 0
    for rank, v in enumerate(videos):
        row = db.one("SELECT id FROM idea_source_items WHERE source_id = ? AND video_id = ?", (source_id, v["video_id"]))
        if row:
            db.execute("UPDATE idea_source_items SET view_count = COALESCE(?, view_count), duration_s = COALESCE(?, duration_s), "
                       "list_rank = ?, title = CASE WHEN title = '' THEN ? ELSE title END WHERE id = ?",
                       (v.get("view_count"), v.get("duration_s"), rank, v.get("title") or "", row["id"]))
            continue
        short = bool(v.get("duration_s")) and v["duration_s"] < min_duration_s
        db.execute("INSERT INTO idea_source_items (id, source_id, video_id, title, published_at, status, error, view_count, "
                   "duration_s, list_rank, is_new, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (new_id("srci"), source_id, v["video_id"], v.get("title") or "", v.get("published_at"),
                    "skipped" if short else "new", "too short" if short else None,
                    v.get("view_count"), v.get("duration_s"), rank, int(mark_new), iso()))
        added += 1
    return added


async def add_channel(core: Any, text: str, *, order_by: str = "views", max_videos: int = 0) -> dict:
    if order_by not in ORDERS:
        raise PolicyRefusal("youtube.order", f"order must be one of {', '.join(ORDERS)}")
    channel_id, url = await resolve_channel(text)
    db = core.db
    existing = db.one("SELECT * FROM idea_sources WHERE kind = 'youtube_channel' AND channel_id = ?", (channel_id,))
    if existing:
        db.execute("UPDATE idea_sources SET status = 'active', order_by = ?, max_videos = ? WHERE id = ?",
                   (order_by, int(max_videos), existing["id"]))
        sid = existing["id"]
        needs_listing = not existing["total_videos"]
    else:
        sid, needs_listing = new_id("src"), True
        with db.tx() as conn:
            insert(conn, "idea_sources", {"id": sid, "kind": "youtube_channel", "url": url, "channel_id": channel_id,
                                          "title": url, "status": "active", "last_checked_at": None, "last_error": None,
                                          "created_at": iso(), "order_by": order_by, "max_videos": int(max_videos),
                                          "total_videos": None, "listing": None})
            log_event(conn, actor_kind="human", kind="human", action="idea_source.add",
                      args={"channel": url, "order_by": order_by, "max_videos": max_videos}, outcome="ok",
                      ref_type="idea_source", ref_id=sid)
    if needs_listing:
        await refresh_listing(core, db.one("SELECT * FROM idea_sources WHERE id = ?", (sid,)), first=True)
    set_setting(db, "youtube:last_check", "")  # start researching on the next poll
    return db.one("SELECT * FROM idea_sources WHERE id = ?", (sid,))


async def refresh_listing(core: Any, src: dict, *, first: bool = False) -> int:
    """Fetch the channel's video list; videos that appear after the first listing are new uploads."""
    db = core.db
    try:
        title, videos, how = await list_videos(core, src["channel_id"])
    except Exception as e:
        db.execute("UPDATE idea_sources SET last_checked_at = ?, last_error = ? WHERE id = ?",
                   (iso(), f"{type(e).__name__}: {str(e)[:200]}", src["id"]))
        if first:
            raise
        return 0
    added = _upsert_items(db, src["id"], videos, mark_new=not first,
                          min_duration_s=int(cfg(core.settings)["min_duration_s"]))
    total = int(db.scalar("SELECT COUNT(*) FROM idea_source_items WHERE source_id = ?", (src["id"],)) or 0)
    db.execute("UPDATE idea_sources SET title = ?, total_videos = ?, listing = ?, last_checked_at = ?, last_error = NULL "
               "WHERE id = ?", (title or src["title"], total, how, iso(), src["id"]))
    set_setting(db, f"youtube:listed:{src['id']}", iso())
    return added


def next_videos(db: Database, src: dict, limit: int) -> list[dict]:
    """New uploads first, then the backlog in the channel's chosen order, within its max_videos."""
    if src["max_videos"]:
        done = int(db.scalar("SELECT COUNT(*) FROM idea_source_items WHERE source_id = ? AND status IN ('sent','failed')",
                             (src["id"],)) or 0)
        limit = min(limit, max(0, src["max_videos"] - done))
        if not limit:
            return []
    order = ("(view_count IS NULL), view_count DESC, list_rank" if src["order_by"] == "views"
             else "list_rank, published_at DESC")
    return db.all(f"SELECT * FROM idea_source_items WHERE source_id = ? AND status = 'new' ORDER BY is_new DESC, {order} "
                  "LIMIT ?", (src["id"], limit))


def _analyst(db: Database) -> dict | None:
    from .ideas import pool_agent
    return pool_agent(db, "venture_analyst")


async def _process(core: Any, src: dict, item: dict) -> str:
    from . import media
    from .ideas import ADAPT_DONT_REJECT
    db, c = core.db, cfg(core.settings)
    if item.get("duration_s") and item["duration_s"] < int(c["min_duration_s"]):
        db.execute("UPDATE idea_source_items SET status = 'skipped', error = 'too short' WHERE id = ?", (item["id"],))
        return "skipped"
    try:
        meta = await media.youtube(core, item["video_id"])
    except PolicyRefusal as e:
        db.execute("UPDATE idea_source_items SET status = 'failed', error = ? WHERE id = ?", (e.detail[:300], item["id"]))
        return "failed"
    if len(meta.get("transcript") or "") < int(c["min_transcript_chars"]):
        db.execute("UPDATE idea_source_items SET status = 'skipped', error = 'transcript too short' WHERE id = ?", (item["id"],))
        return "skipped"
    root = root_org(db)
    title = meta.get("title") or item["title"]
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", f"youtube-{title}")[:70].strip("_") + ".md"
    views = f"{item['view_count']:,} views" if item.get("view_count") else "views unknown"
    aid = media.save_text_artifact(db, core.settings, org_id=root["id"], name=name,
                                   text=f"# {title}\n{meta['url']} | channel: {src['title']} | {views} | "
                                        f"{meta.get('minutes')} min\n\n{meta['transcript']}")
    analyst = _analyst(db)
    task_id = None
    if analyst:
        t = create_task(db, creator=None, assignee_id=analyst["id"], title=f"Ideas from YouTube: {clip(title, 80)}",
                        deliverable="One proposal per distinct business model in this video (adapted to start for $500 "
                                    "or less), submitted with submit_proposal (source 'youtube', source_ref the video URL) "
                                    "with a full business case. Only if a model is illegal/harmful or can't start for $500 "
                                    "even scaled down: one line saying which.",
                        description=(f"The owner subscribed to the channel '{src['title']}'. Video ({views}): {meta['url']}. "
                                     f"Full transcript: read_artifact {aid}. Treat the video's claims (income, ease, "
                                     "demand) as unverified: use conservative numbers from your own research. "
                                     + ADAPT_DONT_REJECT + "The red team reviews every proposal."))
        task_id = t["id"]
    db.execute("UPDATE idea_source_items SET status = 'sent', artifact_id = ?, task_id = ?, title = ? WHERE id = ?",
               (aid, task_id, title, item["id"]))
    return "sent"


async def poll(core: Any, *, force: bool = False) -> int:
    """Every check_hours: re-list channels whose listing is stale, then send the next few videos of each."""
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
        listed = parse_iso(get_setting(db, f"youtube:listed:{src['id']}", ""))
        if force or not listed or (now() - listed).total_seconds() >= float(c["list_every_hours"]) * 3600:
            await refresh_listing(core, src, first=not src["total_videos"])
            src = db.one("SELECT * FROM idea_sources WHERE id = ?", (src["id"],))
        want, done = int(c["max_videos_per_check"]), 0
        for item in next_videos(db, src, want * 3):  # skipped or failed videos don't use up the batch
            if done >= want:
                break
            if await _process(core, src, item) == "sent":
                done += 1
        sent += done
    if sent:
        log_event(db, actor_kind="system", kind="system", action="idea_sources.poll", result={"videos_sent": sent},
                  outcome="ok")
    return sent

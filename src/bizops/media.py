"""Turning owner inputs into text agents can work with: YouTube videos and uploaded files."""

from __future__ import annotations

import asyncio
import hashlib
import io
import mimetypes
import re
from pathlib import Path
from typing import Any

from .db import Database, insert
from .engine import PolicyRefusal
from .util import iso, new_id

YT_RE = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})")
TEXT_EXT = {".txt", ".md", ".csv", ".json", ".html", ".htm", ".xml", ".yaml", ".yml", ".tsv", ".log"}
MAX_UPLOAD = 15_000_000


def youtube_ids(text: str) -> list[str]:
    return list(dict.fromkeys(YT_RE.findall(text or "")))


def _transcript_sync(video_id: str, languages: list[str]) -> list[dict[str, Any]]:
    from youtube_transcript_api import YouTubeTranscriptApi
    fetched = YouTubeTranscriptApi().fetch(video_id, languages=languages)
    return [{"text": s.text, "start": s.start} for s in fetched]


async def youtube(core: Any, url_or_id: str) -> dict[str, Any]:
    """Title, channel and full transcript of a YouTube video (no API key needed)."""
    ids = youtube_ids(url_or_id) or ([url_or_id] if re.fullmatch(r"[A-Za-z0-9_-]{11}", url_or_id or "") else [])
    if not ids:
        raise PolicyRefusal("youtube.url", "not a YouTube video link")
    vid = ids[0]
    meta: dict[str, Any] = {"video_id": vid, "url": f"https://www.youtube.com/watch?v={vid}", "title": "", "channel": ""}
    try:
        page = await core.web.fetch(f"https://www.youtube.com/oembed?format=json&url=https://www.youtube.com/watch?v={vid}")
        import json
        info = json.loads(page["text"])
        meta["title"], meta["channel"] = info.get("title", ""), info.get("author_name", "")
    except Exception:
        pass
    try:
        snippets = await asyncio.to_thread(_transcript_sync, vid, ["en", "en-US", "en-GB"])
    except Exception as e:
        raise PolicyRefusal("youtube.transcript", f"no transcript available for this video ({type(e).__name__}). "
                                                  "Try another video, or ask the owner for a summary.")
    lines, last = [], -60.0
    for s in snippets:
        if s["start"] - last >= 60:  # a timestamp every minute keeps it skimmable
            m, sec = divmod(int(s["start"]), 60)
            lines.append(f"\n[{m}:{sec:02d}]")
            last = s["start"]
        lines.append(s["text"].replace("\n", " "))
    meta["transcript"] = " ".join(lines).strip()
    meta["minutes"] = round((snippets[-1]["start"] if snippets else 0) / 60, 1)
    return meta


def extract_text(data: bytes, name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join((p.extract_text() or "") for p in reader.pages[:200]).strip()
    if ext in (".html", ".htm"):
        from .webclient import html_to_text
        return html_to_text(data.decode("utf-8", "replace"))[1]
    if ext in TEXT_EXT or not ext:
        return data.decode("utf-8", "replace")
    if ext == ".docx":
        import zipfile
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
        return re.sub(r"<[^>]+>", " ", xml.replace("</w:p>", "\n")).strip()
    return ""


def save_upload(db: Database, settings: Any, *, org_id: str, name: str, data: bytes, agent_id: str | None = None) -> dict:
    """Store an owner upload as an artifact (original file + extracted text). Returns the artifact rows."""
    if len(data) > MAX_UPLOAD:
        raise PolicyRefusal("upload.size", "files up to 15 MB")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")[:80] or "upload"
    out = {}
    text = ""
    try:
        text = extract_text(data, safe)
    except Exception:
        text = ""
    for kind, payload, fname in (("file", data, safe), ("text", text.encode(), safe.rsplit(".", 1)[0] + ".txt")):
        if kind == "text" and (not text or Path(safe).suffix.lower() in TEXT_EXT):
            continue
        aid = new_id("art")
        rel = f"{org_id}/{aid}-{fname}"
        path = settings.artifacts_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        with db.tx() as conn:
            insert(conn, "artifacts", {"id": aid, "org_id": org_id, "agent_id": agent_id, "task_id": None, "name": fname,
                                       "path": rel, "mime": mimetypes.guess_type(fname)[0] or "application/octet-stream",
                                       "size_bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
                                       "created_at": iso()})
        out[kind] = {"id": aid, "name": fname}
    out["chars"] = len(text)
    out["preview"] = text[:1500]
    return out


def save_text_artifact(db: Database, settings: Any, *, org_id: str, name: str, text: str) -> str:
    aid = new_id("art")
    rel = f"{org_id}/{aid}-{name}"
    path = settings.artifacts_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode()
    path.write_bytes(data)
    with db.tx() as conn:
        insert(conn, "artifacts", {"id": aid, "org_id": org_id, "agent_id": None, "task_id": None, "name": name, "path": rel,
                                   "mime": "text/markdown", "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                                   "created_at": iso()})
    return aid

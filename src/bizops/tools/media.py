"""Media tools: YouTube transcripts (and owner uploads, which arrive as artifacts readable with read_artifact)."""

from __future__ import annotations

from .. import media
from ..util import clip
from ..webclient import wrap_untrusted
from . import Ctx, ToolOutput, tool


@tool("youtube_transcript", grant="media", action_class="web_read", timeout_s=90, ref_type="artifact",
      description="Get a YouTube video's title, channel and full transcript (saved as a file in your org). Use it to "
                  "study a business idea, a competitor's pitch, or a how-to.",
      properties={"url": {"type": "string"}}, required=["url"])
async def youtube_transcript(ctx: Ctx, args: dict) -> ToolOutput:
    v = await media.youtube(ctx.core, args["url"])
    aid = media.save_text_artifact(ctx.db, ctx.settings, org_id=ctx.agent["org_id"], name=f"youtube-{v['video_id']}.md",
                                   text=f"# {v['title']}\nChannel: {v['channel']}\nURL: {v['url']}\n\n{v['transcript']}")
    body = (f"Title: {v['title']}\nChannel: {v['channel']}\nLength: ~{v['minutes']} min\nSaved as artifact {aid}\n\n"
            f"{clip(v['transcript'], 18000)}")
    return ToolOutput(wrap_untrusted(v["url"], body), ref_type="artifact", ref_id=aid)

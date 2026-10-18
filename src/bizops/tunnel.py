"""Public URL from your own computer, with no account: a Cloudflare quick tunnel.

The URL changes when the tunnel restarts, so this is for trying things out; for an always-on business
deploy to a server (scripts/deploy-fly.sh) or use a named Cloudflare tunnel with your own domain.
"""

from __future__ import annotations

import asyncio
import re
import shutil

URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


async def start_quick_tunnel(port: int, timeout: float = 40) -> tuple[asyncio.subprocess.Process | None, str | None]:
    binary = shutil.which("cloudflared")
    if not binary:
        return None, None
    proc = await asyncio.create_subprocess_exec(binary, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}",
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=max(0.1, deadline - loop.time()))
        except TimeoutError:
            break
        if not line:
            break
        m = URL.search(line.decode(errors="replace"))
        if m:
            asyncio.create_task(_drain(proc))
            return proc, m.group(0)
    return proc, None


async def _drain(proc: asyncio.subprocess.Process) -> None:
    while proc.stdout and await proc.stdout.readline():
        pass

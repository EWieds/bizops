"""The server stays up under load: CLI calls are bounded and always reaped, and idle browsers are closed."""

import asyncio
import os
import sys

import pytest


async def test_reap_kills_the_whole_process_group():
    from bizops.llm.claude_cli import _reap
    # A parent that starts a long-lived child, like the claude CLI (Node) spawning helpers.
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import subprocess, time; subprocess.Popen(['sleep', '300']); time.sleep(300)",
        start_new_session=True)
    await asyncio.sleep(0.5)
    await _reap(proc)
    assert proc.returncode is not None  # waited for: no zombie
    await asyncio.sleep(0.3)
    assert not running_in_group(proc.pid)  # the child was killed too (at most a zombie for init to collect)


def running_in_group(pgid: int) -> list[int]:
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            stat = open(f"/proc/{d}/stat").read()
        except OSError:
            continue
        fields = stat.rsplit(")", 1)[1].split()
        if int(fields[2]) == pgid and fields[0] != "Z":
            out.append(int(d))
    return out


async def test_cancelled_cli_call_is_reaped(settings, monkeypatch, tmp_path):
    from bizops.llm import claude_cli
    script = tmp_path / "fake_claude"
    script.write_text("#!/bin/sh\nsleep 300\n")
    script.chmod(0o755)
    client = claude_cli.ClaudeCLIClient(settings, binary=str(script))
    started = []
    real = asyncio.create_subprocess_exec

    async def spy(*a, **kw):
        p = await real(*a, **kw)
        started.append(p)
        return p
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    task = asyncio.create_task(client._call(prompt="hi", system="s", model="m", schema={}))
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert started and started[0].returncode is not None


async def test_cli_concurrency_is_bounded(settings, tmp_path):
    from bizops.llm import claude_cli
    settings.models["cli_max_concurrent"] = 2
    script = tmp_path / "fake_claude"
    script.write_text('#!/bin/sh\ncat >/dev/null\nsleep 0.4\necho \'{"type":"result","subtype":"success","result":"ok"}\'\n')
    script.chmod(0o755)
    client = claude_cli.ClaudeCLIClient(settings, binary=str(script))
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await asyncio.gather(*(client._call(prompt="p", system="s", model="m", schema={}) for _ in range(4)))
    assert loop.time() - t0 >= 0.75  # 4 calls through 2 slots take two rounds


class FakeCtx:
    def __init__(self):
        self.closed = False

    def set_default_timeout(self, ms):
        pass

    async def route(self, pattern, handler):
        pass

    async def close(self):
        self.closed = True


async def test_browser_keeps_only_a_few_profiles_open(settings, monkeypatch):
    from bizops.browser import Browser
    settings.policy.setdefault("browser", {})["max_open_profiles"] = 2
    b = Browser(settings)
    made = []

    class Chromium:
        async def launch_persistent_context(self, path, **kw):
            made.append(FakeCtx())
            return made[-1]

    class PW:
        chromium = Chromium()
    b._pw = PW()
    monkeypatch.setattr(b, "installed", lambda: True)
    for org in ("org_a", "org_b", "org_c"):
        await b._context(org)
    assert list(b._contexts) == ["org_b", "org_c"] and made[0].closed  # least recently used closed
    await b._context("org_b")
    await b._context("org_d")
    assert set(b._contexts) == {"org_b", "org_d"}


async def test_idle_browsers_close(settings, monkeypatch):
    import time

    from bizops.browser import Browser
    b = Browser(settings)
    ctx = FakeCtx()
    b._contexts["org_a"], b._ctx_used["org_a"] = ctx, time.monotonic() - 4000
    assert await b.close_idle(max_idle_s=1800) == 1
    assert ctx.closed and not b._contexts

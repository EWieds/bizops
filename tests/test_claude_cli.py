import json
import os
import stat

from bizops.core import Core, detect_backend
from bizops.engine import human_message
from bizops.llm.claude_cli import ClaudeCLIClient, _render_transcript
from bizops.runtime import run_agent

FAKE = r'''#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
if args[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True})); sys.exit(0)
prompt = sys.stdin.read()
schema = json.loads(args[args.index("--json-schema") + 1])
open(sys.argv[0] + ".log", "a").write(json.dumps({"args": args, "prompt": prompt[:200]}) + "\n")
if "tool_calls" in schema["properties"]:
    if "(result for" in prompt:
        out = {"message": "done", "tool_calls": [{"name": "yield_run", "input_json": json.dumps({"summary": "all good"})}]}
    else:
        out = {"message": "noting", "tool_calls": [
            {"name": "note_write", "input_json": json.dumps({"key": "k", "body": "b", "reason": "test"})},
            {"name": "note_read", "input_json": "not json"}]}
elif "results" in schema["properties"]:
    out = {"summary": "s", "results": [{"title": "T", "url": "https://example.com"}]}
else:
    out = {"verdict": "allow", "confidence": 0.9, "reason": "ok"}
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "structured_output": out,
                  "total_cost_usd": 0.0123, "usage": {"input_tokens": 100, "output_tokens": 20,
                  "cache_read_input_tokens": 50, "cache_creation_input_tokens": 0}}))
'''


def fake_claude(tmp_path):
    p = tmp_path / "claude"
    p.write_text(FAKE)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


def test_transcript_rendering():
    t = _render_transcript([{"role": "user", "content": "hi"},
                            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}]},
                            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}])
    assert "(called x id=t1)" in t and "(result for t1)" in t and "never invoke them natively" in t


async def test_cli_client_runs_an_agent_through_the_gateway(settings, tmp_path, monkeypatch):
    binary = fake_claude(tmp_path)
    settings.dry_run = False
    core = Core(settings, llm=ClaudeCLIClient(settings, binary=str(binary))).init()
    from bizops.seed import ensure_platform
    from bizops.engine import org_ceo
    root = ensure_platform(core)
    ceo = org_ceo(core.db, root)
    human_message(core.db, settings, ceo["id"], "status?")
    rid = await run_agent(core, ceo["id"], "message")
    run = core.db.one("SELECT * FROM runs WHERE id = ?", (rid,))
    assert run["status"] == "yielded" and run["turns"] == 2
    assert run["cost_micros"] == 2 * 12_300  # exact CLI-reported cost
    assert core.db.scalar("SELECT body FROM memory_notes WHERE key = 'k'") == "b"
    bad = core.db.one("SELECT * FROM events WHERE action = 'note_read' AND phase = 'after'")
    assert bad  # invalid JSON input still went through the gateway (handled, not crashed)
    log = [json.loads(line) for line in open(str(binary) + ".log")]
    first = log[0]["args"]
    assert "--tools" in first and first[first.index("--tools") + 1] == "" and "--no-session-persistence" in first


async def test_cli_search_and_structured(settings, tmp_path):
    c = ClaudeCLIClient(settings, binary=str(fake_claude(tmp_path)))
    res = await c.web_search(query="lawn care", max_results=3)
    assert res.results[0]["url"] == "https://example.com" and res.cost_micros == 12_300
    data, _ = await c.structured(model="m", system="s", prompt="p", schema={"type": "object", "properties": {}})
    assert data["verdict"] == "allow"


def test_detect_backend(settings, tmp_path, monkeypatch):
    settings.dry_run = False
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    fake_claude(tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    assert detect_backend(settings)[0] == "claude_cli"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert detect_backend(settings)[0] == "api"
    settings.dry_run = True
    assert detect_backend(settings)[0] == "dry_run"


def test_reset_parsing():
    from bizops.llm.claude_cli import _reset_in
    assert 60 < _reset_in("You've hit your session limit · resets 9:30pm (UTC)") <= 86400 + 60
    assert _reset_in("weird") == 1800


async def test_rate_limit_pauses_everyone_and_resumes(core, org, monkeypatch):
    from bizops.engine import get_setting, human_message
    from bizops.llm.base import LLMRateLimited
    from bizops.runtime import run_agent
    from bizops.scheduler import Scheduler

    async def limited(**kw):
        raise LLMRateLimited("session limit", retry_after_s=600)
    monkeypatch.setattr(core.llm, "complete", limited)
    human_message(core.db, core.settings, org["ceo"]["id"], "hi")
    rid = await run_agent(core, org["ceo"]["id"], "message")
    assert core.db.scalar("SELECT status FROM runs WHERE id = ?", (rid,)) == "rate_limited"
    assert get_setting(core.db, "llm_paused_until")
    human_message(core.db, core.settings, org["cfo"]["id"], "hi")
    assert Scheduler(core).due_agents() == []
    core.db.execute("UPDATE settings SET value = '2000-01-01T00:00:00Z' WHERE key = 'llm_paused_until'")
    assert Scheduler(core).due_agents()

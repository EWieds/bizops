"""Signing secret, login lockout, server-side sessions, CSRF, client IP, public demo budget, file links."""

import os
import re

import pytest
from fastapi.testclient import TestClient

from bizops import business
from bizops.security import MissingSecret, check_secret
from bizops.util import iso, now
from bizops.web.app import create_app
from bizops.web.auth import set_password

PW = "correct horse battery"


@pytest.fixture
def app(core, org):
    return create_app(core, run_scheduler=False)


def csrf_of(client) -> str:
    return re.search(r'name="csrf-token" content="([^"]*)"', client.get("/system").text).group(1)


def login(client, pw=PW, **kw):
    return client.post("/login", data={"password": pw}, follow_redirects=False, **kw)


@pytest.mark.parametrize("value", [None, "", "dev", "short-secret"])
def test_refuses_to_start_without_a_real_secret(core, value):
    if value is None:
        os.environ.pop("BIZOPS_SECRET", None)
    else:
        os.environ["BIZOPS_SECRET"] = value
    with pytest.raises(MissingSecret):
        check_secret()
    with pytest.raises(MissingSecret):
        create_app(core, run_scheduler=False)
    with pytest.raises(MissingSecret):  # and nothing gets signed with a fallback either
        business.unsub_token(core.settings, "a@b.com")


def test_cli_dry_run_gets_temporary_secret_live_refuses(settings, monkeypatch):
    from bizops import cli
    monkeypatch.delenv("BIZOPS_SECRET", raising=False)
    cli._require_secret(settings)  # settings fixture is a dry run
    assert len(os.environ["BIZOPS_SECRET"]) >= 32
    monkeypatch.delenv("BIZOPS_SECRET")
    settings.dry_run = False
    with pytest.raises(SystemExit):
        cli._require_secret(settings)


def test_lockout_after_five_failures(core, app):
    set_password(core.settings.home / ".env", PW)
    with TestClient(app) as c:
        for _ in range(5):
            assert login(c, "wrong").status_code == 401
        r = login(c)  # even the right password is refused while locked
        assert r.status_code == 429 and "Too many attempts" in r.text
        # Another address isn't locked out (Fly's proxy header is trusted only on Fly).
        os.environ["FLY_APP_NAME"] = "bizops-test"
        assert login(c, headers={"fly-client-ip": "203.0.113.9"}).status_code == 303
        os.environ.pop("FLY_APP_NAME")
        # 15 minutes later the lock lifts.
        core.db.execute("UPDATE login_attempts SET ts = ?", (iso(now().replace(year=2000)),))
        assert login(c).status_code == 303


def test_spoofed_proxy_header_is_ignored_off_fly(core, app):
    set_password(core.settings.home / ".env", PW)
    with TestClient(app) as c:
        for i in range(5):  # rotating a fake header doesn't dodge the lockout
            login(c, "wrong", headers={"fly-client-ip": f"198.51.100.{i}"})
        assert login(c, headers={"fly-client-ip": "198.51.100.200"}).status_code == 429


def test_sessions_are_server_side_and_revocable(core, app):
    set_password(core.settings.home / ".env", PW)
    with TestClient(app) as a, TestClient(app) as b:
        login(a)
        login(b)
        assert a.get("/", follow_redirects=False).status_code == 200
        stolen = a.cookies.get("bizops_session")
        assert core.db.scalar("SELECT COUNT(*) AS n FROM sessions WHERE id = ?", (stolen,)) == 0  # only the hash is stored
        token = csrf_of(a)
        # "Log out all other sessions" from browser A kills B.
        assert a.post("/sessions/revoke-others", headers={"X-CSRF-Token": token}, follow_redirects=False).status_code == 303
        assert b.get("/", follow_redirects=False).status_code == 303
        assert a.get("/", follow_redirects=False).status_code == 200
        # Logging out revokes the cookie on the server: replaying it no longer works.
        a.post("/logout")
        with TestClient(app, cookies={"bizops_session": stolen}) as thief:
            assert thief.get("/", follow_redirects=False).status_code == 303
        # Changing the password logs everyone out.
        login(a)
        set_password(core.settings.home / ".env", "another long password", db=core.db)
        assert a.get("/", follow_redirects=False).status_code == 303


def test_csrf_required_on_every_post(core, org, app):
    set_password(core.settings.home / ".env", PW)
    with TestClient(app) as c:
        login(c)
        token = csrf_of(c)
        agent = org["ceo"]["id"]
        assert c.post("/pause", data={"target": "platform"}, follow_redirects=False).status_code == 403
        assert c.post("/pause", data={"target": "platform"}, headers={"X-CSRF-Token": "forged"}, follow_redirects=False).status_code == 403
        # A form field works too, and the route still sees the form (body is replayed after the check).
        r = c.post("/chat", data={"agent": agent, "text": "hello via form", "csrf_token": token}, follow_redirects=False)
        assert r.status_code == 303
        assert core.db.one("SELECT 1 AS x FROM messages WHERE body = 'hello via form'")
        r = c.post("/chat", data={"agent": agent, "text": "hello multipart", "csrf_token": token},
                   files={"files": ("n.txt", b"notes", "text/plain")}, follow_redirects=False)
        assert r.status_code == 303 and core.db.one("SELECT 1 AS x FROM messages WHERE body LIKE 'hello multipart%'")


def test_csrf_without_password_uses_double_submit_cookie(core, org, app):
    with TestClient(app) as c:
        assert c.post("/pause", data={"target": "platform"}, follow_redirects=False).status_code == 403  # a cross-site POST to localhost
        token = csrf_of(c)
        assert token and c.cookies.get("bizops_csrf") == token
        assert c.post("/pause", data={"target": "platform", "paused": "0"}, headers={"X-CSRF-Token": token}, follow_redirects=False).status_code == 303


async def test_public_demo_is_budget_gated_before_the_model_call(core, org, app):
    va = await business.upsert_voice_agent(core, agent=org["ceo"], cfg={"business_name": "Joe's HVAC"})
    vid = va["voice_agent_id"]
    calls = []
    real = __import__("bizops.tools.sales", fromlist=["voice_reply"]).voice_reply

    async def counting(core_, cfg, history):
        calls.append(1)
        return await real(core_, cfg, history)
    import bizops.tools.sales as sales
    sales.voice_reply = counting
    try:
        say = {"history": [{"role": "user", "content": "open Saturday?"}]}
        with TestClient(app) as c:
            assert "Joe's HVAC" in c.post(f"/demo/{vid}/say", json=say).json()["reply"] and calls == [1]
            core.db.execute("UPDATE orgs SET monthly_cap_micros = 0 WHERE id = ?", (org["org"],))
            assert "resting" in c.post(f"/demo/{vid}/say", json=say).json()["reply"]
            assert calls == [1]  # refused before any model call
            assert core.db.one("SELECT 1 AS x FROM events WHERE action = 'public.demo' AND outcome = 'refused'")
    finally:
        sales.voice_reply = real


async def test_demo_daily_ceiling(core, org, app):
    from bizops import ledger
    va = await business.upsert_voice_agent(core, agent=org["ceo"], cfg={"business_name": "Joe's HVAC"})
    ledger.record(core.db, org_id=org["org"], kind="model_cost", amount_micros=2_000_000, source="model",
                  memo="public demo", ref_id="public:demo")
    with TestClient(app) as c:
        r = c.post(f"/demo/{va['voice_agent_id']}/say", json={"history": [{"role": "user", "content": "hi"}]})
        assert "resting" in r.json()["reply"]


def test_shared_file_links_use_their_own_key(core, org, app):
    aid = "art_x"
    core.db.execute("INSERT INTO artifacts (id, org_id, agent_id, name, path, mime, size_bytes, sha256, created_at) "
                    "VALUES (?, ?, ?, 'r.txt', 'r.txt', 'text/plain', 2, '', ?)", (aid, org["org"], org["ceo"]["id"], iso()))
    core.settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
    (core.settings.artifacts_dir / "r.txt").write_text("ok")
    old_style = business.unsub_token(core.settings, aid).split(".")[1]
    with TestClient(app) as c:
        assert c.get(f"/f/{aid}/{old_style}").status_code == 404  # an unsubscribe signature is not a file key
        assert c.get(f"/f/{aid}/{business.file_sig(aid)}").text == "ok"
        assert c.get(f"/f/{aid}/{business.file_sig(aid)[:-1]}x").status_code == 404

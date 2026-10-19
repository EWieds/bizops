"""The agents' browser, end to end against a local signup + checkout page (card field in an iframe)."""

import os
import threading
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler

import pytest

from bizops.browser import Browser
from bizops.payments import deposit
from bizops.tools import Ctx
from bizops.gateway import invoke

pytestmark = pytest.mark.skipif(not Browser.installed(), reason="playwright not installed")

CHROMIUM = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"

INDEX = """<!doctype html><title>Signup</title><h1>Create your account</h1>
<form onsubmit="event.preventDefault(); const f = frames[0].document.getElementById('cc').value;
  document.body.innerHTML = '<p id=done>Welcome ' + document.getElementById('email').value +
  ' card ok=' + (f === '4111111111111111') + ' pw ok=' + (document.getElementById('pw').value.length > 10) +
  ' plan=' + document.getElementById('plan').value + '</p>';">
<label for=email>Email</label><input id=email type=email>
<label for=pw>Password</label><input id=pw type=password>
<select id=plan aria-label=Plan><option value=free>Free</option><option value=pro>Pro</option></select>
<iframe src="/card.html"></iframe>
<button type=submit>Sign up</button></form>"""
CARD = """<!doctype html><input id=cc aria-label="Card number" autocomplete=cc-number>"""


@pytest.fixture
def site(tmp_path):
    (tmp_path / "www").mkdir()
    (tmp_path / "www" / "index.html").write_text(INDEX)
    (tmp_path / "www" / "card.html").write_text(CARD)
    srv = HTTPServer(("127.0.0.1", 0), partial(_Quiet, directory=str(tmp_path / "www")))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/"
    srv.shutdown()


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


@pytest.fixture
async def browser_core(core, org, monkeypatch):
    if os.path.exists(CHROMIUM):
        monkeypatch.setenv("BIZOPS_CHROMIUM", CHROMIUM)
    yield core
    await core.browser.aclose()


def _ref(snapshot: str, label: str) -> str:
    line = next(ln for ln in snapshot.splitlines() if f'"{label}"' in ln and ln.startswith("["))
    return line[1:line.index("]")]


async def test_signup_and_checkout_with_hidden_secrets(browser_core, org, site):
    core = browser_core
    core.settings.policy["browser"]["allow_private_hosts"] = True
    ceo = Ctx(core=core, agent=org["ceo"])
    cio = core.db.one("SELECT * FROM agents WHERE role = 'platform_cfo'")
    deposit(core.db, org_id=org["org"], amount_usd=50)
    assert (await invoke(ceo, "request_purchase", {"vendor": "Local", "item": "Pro", "amount_usd": 20,
                                                   "period": "once", "why": "test"})).ok
    pid = core.db.scalar("SELECT id FROM purchases")
    assert (await invoke(Ctx(core=core, agent=cio), "decide_purchase",
                         {"purchase_id": pid, "decision": "approve", "why": "fine"})).ok
    assert (await invoke(ceo, "save_login", {"name": "local", "site": site, "username": "hello+co@example.com"})).ok

    r = await invoke(ceo, "browser_open", {"url": site})
    assert r.ok, r.content
    snap = r.content
    assert "Create your account" in snap and "f1e0" in snap
    await invoke(ceo, "browser_type", {"ref": _ref(snap, "Email"), "text": "{{login:local:username}}"})
    r = await invoke(ceo, "browser_type", {"ref": _ref(snap, "Password"), "text": "{{login:local:password}}"})
    assert 'value="(filled)"' in r.content
    await invoke(ceo, "browser_select", {"ref": _ref(snap, "Plan"), "option": "Pro"})
    r = await invoke(ceo, "browser_type", {"ref": "f1e0", "text": f"{{{{card:{pid}:number}}}}"})
    assert r.ok, r.content
    assert "4111111111111111" not in r.content and "•••" in r.content
    r = await invoke(ceo, "browser_click", {"ref": _ref(snap, "Sign up")})
    assert "Welcome hello+co@example.com card ok=true pw ok=true plan=pro" in r.content

    secret = core.db.scalar("SELECT secret FROM credentials WHERE name = 'local'")
    logged = str(core.db.all("SELECT * FROM events")) + str(core.db.all("SELECT * FROM runs"))
    assert secret not in logged and "4111111111111111" not in logged


async def test_browser_refuses_private_addresses(browser_core, org, site):
    r = await invoke(Ctx(core=browser_core, agent=org["ceo"]), "browser_open", {"url": site})
    assert not r.ok and "web.ssrf" in r.content


async def test_card_placeholder_needs_approved_purchase(browser_core, org, site):
    core = browser_core
    core.settings.policy["browser"]["allow_private_hosts"] = True
    ceo = Ctx(core=core, agent=org["ceo"])
    await invoke(ceo, "browser_open", {"url": site})
    r = await invoke(ceo, "browser_type", {"ref": "f1e0", "text": "{{card:pur_fake:number}}"})
    assert not r.ok and "purchase.unknown" in r.content

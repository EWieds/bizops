import pytest
from fastapi.testclient import TestClient

from bizops import business
from bizops.web.app import create_app
from bizops.web.auth import set_password


@pytest.fixture
def client(core, org):
    with TestClient(create_app(core, run_scheduler=False)) as c:
        yield c


def test_public_routes_need_no_login_and_dashboard_does(core, org, client, monkeypatch):
    set_password(core.settings.home / ".env", "correct horse battery")
    assert client.get("/", follow_redirects=False).status_code == 303
    business.publish_page(core.db, agent=org["ceo"], path="index", title="Home", html_body="<h1>Hello</h1>")
    slug = core.db.scalar("SELECT slug FROM orgs WHERE id = ?", (org["org"],))
    r = client.get(f"/s/{slug}/")
    assert r.status_code == 200 and "Hello" in r.text
    assert client.post("/login", data={"password": "wrong"}).status_code == 401
    client.post("/login", data={"password": "correct horse battery"})
    assert client.get("/").status_code == 200
    assert client.get("/connect").status_code == 200 and client.get("/pipeline").status_code == 200


def test_remote_without_password_is_locked(core, org, client):
    r = client.get("/", headers={"x-forwarded-for": "8.8.8.8"})
    assert r.status_code == 403


async def test_demo_and_test_checkout_and_unsubscribe(core, org, client):
    va = await business.upsert_voice_agent(core, agent=org["ceo"], cfg={"business_name": "Joe's HVAC", "hours": "9-5"})
    vid = va["voice_agent_id"]
    assert client.get(f"/demo/{vid}").status_code == 200
    r = client.post(f"/demo/{vid}/say", json={"history": [{"role": "user", "content": "Are you open Saturday?"}]})
    assert "Joe's HVAC" in r.json()["reply"]
    offer = await business.create_offer(core, agent=org["ceo"], name="Plan", description="d", price_usd=49, interval="month")
    lead = business.add_lead(core.db, org_id=org["org"], agent=org["ceo"], company="Z", email="z@z.com", source_url="https://z.com")
    assert "TEST CHECKOUT" in client.get(f"/pay/{offer['id']}?ref={lead['id']}").text
    client.post(f"/pay/{offer['id']}", data={"ref": lead["id"], "email": "z@z.com"})
    assert core.db.scalar("SELECT COUNT(*) AS n FROM customers WHERE lead_id = ?", (lead["id"],)) == 1
    tok = business.unsub_token(core.settings, "z@z.com")
    assert client.get(f"/u/{tok}").status_code == 200
    assert core.db.one("SELECT 1 AS x FROM suppressions WHERE email = 'z@z.com'")
    assert client.get("/u/bogus.token").status_code == 404

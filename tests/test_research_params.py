"""The owner's research parameters steer the venture analyst; the hard limits are enforced in code."""

import copy
import json

import pytest
from fastapi.testclient import TestClient

from conftest import with_csrf
from test_ideas_chat import PROPOSAL, analyst

from bizops import ideas
from bizops.engine import PolicyRefusal
from bizops.llm.dry_run import DEMO_CASE
from bizops.web.app import create_app

PARAMS = {"focus": "Home services and dental practices", "avoid": "crypto, gambling, health claims",
          "geography": "Ohio", "customer_type": "b2b", "risk_tolerance": "low",
          "max_startup_usd": "100", "max_days_to_first_revenue": "30", "min_price_usd": "50"}


def test_set_params_validates_and_clears(core, org):
    out = ideas.set_params(core.db, PARAMS)
    assert out["max_startup_usd"] == 100.0 and out["customer_type"] == "b2b"
    with pytest.raises(PolicyRefusal):
        ideas.set_params(core.db, {"customer_type": "aliens"})
    with pytest.raises(PolicyRefusal):
        ideas.set_params(core.db, {"max_startup_usd": "lots"})
    assert ideas.set_params(core.db, {"focus": "", "max_startup_usd": ""}) == {}
    assert ideas.get_params(core.db) == {}


def test_hard_limits_refuse_research_but_only_flag_owner_ideas(core, org):
    ideas.set_params(core.db, PARAMS)
    cheap = submit_ok = ideas.submit_proposal(core.db, core.settings, author=analyst(core), fields=PROPOSAL)
    assert cheap["status"] == "red_team"  # $20 startup, 21 days, $149: inside every limit
    case = copy.deepcopy(DEMO_CASE)
    case["costs"]["startup_usd"] = 500
    case["time_to_first_revenue_days"] = 60
    with pytest.raises(PolicyRefusal) as e:
        ideas.submit_proposal(core.db, core.settings, author=analyst(core),
                              fields={**PROPOSAL, "title": "Expensive", "business_case": case})
    assert e.value.rule == "proposal.params" and "startup cost $500" in e.value.detail and "60 days" in e.value.detail
    p = ideas.submit_proposal(core.db, core.settings, author=analyst(core),
                              fields={**PROPOSAL, "title": "Owner's pricey idea", "business_case": case, "source": "owner"})
    assert any("breaks the owner's own limit" in f for f in json.loads(p["checks_json"]))
    # A revision can't sneak past the limits either.
    core.db.execute("UPDATE proposals SET status = 'revising' WHERE id = ?", (submit_ok["id"],))
    with pytest.raises(PolicyRefusal) as e:
        ideas.revise_proposal(core.db, core.settings, author=analyst(core), proposal_id=submit_ok["id"],
                              fields={"business_case": case}, response="bigger plan")
    assert e.value.rule == "proposal.params"


def test_month6_profit_floor(core, org):
    ideas.set_params(core.db, {"min_month6_profit_usd": "5000"})
    with pytest.raises(PolicyRefusal) as e:
        ideas.submit_proposal(core.db, core.settings, author=analyst(core), fields=PROPOSAL)
    assert "month-6 profit $1,429" in e.value.detail


def test_params_reach_the_analyst_red_team_and_ceo(core, org):
    from bizops.runtime import gather_context
    ideas.set_params(core.db, PARAMS)
    core.settings.policy["venture"]["research_every_days"] = 3
    ideas.research_cycle(core.db, core.settings)
    task = core.db.one("SELECT * FROM tasks WHERE title LIKE 'Research cycle%'")
    assert "Home services and dental practices" in task["description"] and "HARD LIMIT, Max startup cost" in task["description"]
    p = ideas.submit_proposal(core.db, core.settings, author=analyst(core), fields=PROPOSAL)
    rt_task = core.db.one("SELECT * FROM tasks WHERE title LIKE ?", (f"Red-team {p['id']}%",))
    assert "crypto, gambling" in rt_task["description"] and "fits the owner's research parameters" in rt_task["description"]
    for role in ("venture_analyst", "red_team", "platform_ceo"):
        a = core.db.one("SELECT * FROM agents WHERE role = ?", (role,))
        assert "Geography" not in gather_context(core, a, "timer")[0]  # labels are the human ones
        assert "Where the customers should be: Ohio" in gather_context(core, a, "timer")[0]
    sales = org["sales"] if "sales" in org else org["ceo"]
    assert "research parameters" not in gather_context(core, sales, "timer")[0]


def test_dashboard_form(core, org):
    with TestClient(create_app(core, run_scheduler=False)) as c:
        with_csrf(c)
        assert "Research parameters" in c.get("/ideas").text
        r = c.post("/ideas/params", data={**PARAMS, "research_now": "1"}, follow_redirects=False)
        assert r.status_code == 303 and "saved" in r.headers["location"]
        assert ideas.get_params(core.db)["geography"] == "Ohio"
        assert core.db.one("SELECT 1 AS x FROM tasks WHERE title LIKE 'Research cycle%'")  # research started right away
        page = c.get("/ideas?saved=1").text
        assert "Home services and dental practices" in page and 'value="100"' in page
        assert c.post("/ideas/params", data={"customer_type": "x"}).status_code == 400

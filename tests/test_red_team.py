"""Business cases are complete and checked in code; every proposal is red-teamed before it can launch,
owner ideas hardest; agents launch only what survives."""

import copy
import json

import pytest
from fastapi.testclient import TestClient

from conftest import with_csrf
from test_ideas_chat import PROPOSAL, analyst, red_team

from bizops import ideas
from bizops.engine import PolicyRefusal
from bizops.gateway import invoke
from bizops.llm.dry_run import DEMO_CASE
from bizops.tools import Ctx
from bizops.web.app import create_app


def rt_agent(core):
    return core.db.one("SELECT * FROM agents WHERE role = 'red_team'")


def submit(core, **over):
    return ideas.submit_proposal(core.db, core.settings, author=analyst(core), fields={**PROPOSAL, **over})


# ---------------------------------------------------------------- the business case

@pytest.mark.parametrize("path, value, needle", [
    (("pricing", "price_usd"), None, "price_usd"),
    (("pricing", "competitor_prices"), [{"name": "X", "price": "$1"}], "source URLs"),
    (("acquisition", "first_customers"), "small businesses", "first 10 customers"),
    (("projections",), [{"month": 1, "customers": 1, "revenue_usd": 1, "costs_usd": 1}], "projections"),
    (("assumptions",), [], "assumptions"),
    (("risks",), ["one"], "risks"),
])
def test_business_case_must_be_complete(core, org, path, value, needle):
    case = copy.deepcopy(DEMO_CASE)
    target = case
    for k in path[:-1]:
        target = target[k]
    target[path[-1]] = value
    with pytest.raises(PolicyRefusal) as e:
        submit(core, business_case=case)
    assert needle in e.value.detail
    with pytest.raises(PolicyRefusal):
        submit(core, title="No case", business_case=None)


def test_arithmetic_is_recomputed_and_optimism_flagged(core, org):
    case = copy.deepcopy(DEMO_CASE)
    case["projections"][0]["profit_usd"] = 5000          # wrong arithmetic
    case["projections"][1]["revenue_usd"] = 9000         # 3 customers x $149 can't make $9,000
    case["unit_economics"]["cac_usd"] = 400              # LTV/CAC 900/400 = 2.25
    case["time_to_first_revenue_days"] = 45
    p = submit(core, business_case=case)
    flags = json.loads(p["checks_json"])
    text = " ".join(flags)
    assert "stated profit" in text and "exceeds customers x price" in text and "LTV/CAC" in text and "45 days" in text
    stored = json.loads(p["business_case_json"])
    assert stored["projections"][0]["profit_usd"] == 149 - 60  # recomputed, not trusted
    assert stored["projections"][0]["cumulative_profit_usd"] == 149 - 60 - 20  # minus startup cost
    assert stored["breakeven_month"] == 1
    # The red team's task carries the flags as starting points.
    task = core.db.one("SELECT * FROM tasks WHERE assignee_id = ?", (rt_agent(core)["id"],))
    assert "LTV/CAC" in task["description"] and "Your job is to find why this fails" in task["description"]


# ---------------------------------------------------------------- the red team

def test_red_team_must_be_thorough_and_independent(core, org):
    p = submit(core)
    with pytest.raises(PolicyRefusal) as e:  # thin reviews are refused
        red_team(core, p["id"], risks=[{"risk": "one"}])
    assert e.value.rule == "redteam.thin"
    with pytest.raises(PolicyRefusal) as e:
        red_team(core, p["id"], counter_evidence=[{"claim": "no link"}])
    assert e.value.rule == "redteam.thin"
    with pytest.raises(PolicyRefusal) as e:
        red_team(core, p["id"], revised_scores={"demand": 2})
    assert e.value.rule == "redteam.scores"
    with pytest.raises(PolicyRefusal) as e:
        red_team(core, p["id"], verdict="go", fatal_flaws=["nobody pays"])
    assert e.value.rule == "redteam.contradiction"
    with pytest.raises(PolicyRefusal) as e:  # only the red team reviews
        ideas.record_red_team(core.db, core.settings, reviewer=analyst(core), proposal_id=p["id"], review={})
    assert e.value.rule == "redteam.role"
    with pytest.raises(PolicyRefusal) as e:  # and it never writes proposals itself
        ideas.submit_proposal(core.db, core.settings, author=rt_agent(core), fields={**PROPOSAL, "title": "Mine"})
    assert e.value.rule == "proposal.independence"


def test_owner_ideas_get_the_hardest_review(core, org):
    p = submit(core, title="Owner's idea", source="owner")
    task = core.db.one("SELECT * FROM tasks WHERE title LIKE ?", (f"Red-team {p['id']}%",))
    assert "OWNER'S OWN IDEA" in task["description"] and "No deference" in task["description"]
    three = [{"risk": f"r{i}", "likelihood": "high", "impact": "high", "mitigation": "m"} for i in range(3)]
    with pytest.raises(PolicyRefusal) as e:  # enough for a research idea, not for the owner's
        red_team(core, p["id"], risks=three)
    assert "at least 5" in e.value.detail
    with pytest.raises(PolicyRefusal) as e:
        red_team(core, p["id"], counter_evidence=[{"claim": "c", "source_url": "https://example.com"}])
    assert "at least 2" in e.value.detail
    red_team(core, p["id"], verdict="no_go")
    msg = core.db.one("SELECT body FROM messages WHERE body LIKE 'Reviewed proposal%' ORDER BY created_at DESC")
    assert "parked" in msg["body"] and "owner's own idea" in msg["body"]


def test_fix_revise_loop_and_round_limit(core, org):
    core.settings.policy["venture"]["red_team_max_rounds"] = 2
    p = submit(core)
    red_team(core, p["id"], verdict="fix")
    assert core.db.scalar("SELECT status FROM proposals WHERE id = ?", (p["id"],)) == "revising"
    assert core.db.one("SELECT 1 AS x FROM messages WHERE recipient_id = ? AND body LIKE '%Required changes: cut the reply-rate%'",
                       (analyst(core)["id"],))
    with pytest.raises(PolicyRefusal):  # a fix verdict without the changes is refused
        p2 = submit(core, title="Other")
        red_team(core, p2["id"], verdict="fix", required_changes=[])
    case = copy.deepcopy(DEMO_CASE)
    case["acquisition"]["channels"][0]["expected_conversion_pct"] = 1
    with pytest.raises(PolicyRefusal):
        ideas.revise_proposal(core.db, core.settings, author=analyst(core), proposal_id=p["id"], fields={}, response="")
    r = ideas.revise_proposal(core.db, core.settings, author=analyst(core), proposal_id=p["id"],
                              fields={"business_case": case}, response="Cut conversion to 1%, as asked.")
    assert r["status"] == "red_team" and r["review_round"] == 2
    task = core.db.one("SELECT * FROM tasks WHERE title LIKE ? ORDER BY created_at DESC", (f"Red-team {p['id']}%",))
    assert "Cut conversion to 1%" in task["description"] and "round 2" in task["description"]
    red_team(core, p["id"], verdict="fix")  # out of rounds: parked, not looped forever
    assert core.db.scalar("SELECT status FROM proposals WHERE id = ?", (p["id"],)) == "parked"


# ---------------------------------------------------------------- launching

async def test_agents_launch_only_what_survives(core, org):
    core.settings.policy["platform"]["monthly_cap_usd"] = 1000
    c = Ctx(core=core, agent=org["pceo"])

    async def approve(pid):
        return await invoke(c, "decide_proposal", {"proposal_id": pid, "decision": "approve", "reason": "r"})

    unreviewed = submit(core, title="Unreviewed")
    r = await approve(unreviewed["id"])
    assert not r.ok and "red team" in r.content
    nogo = submit(core, title="No go")
    red_team(core, nogo["id"], verdict="no_go")
    assert not (await approve(nogo["id"])).ok
    weak = submit(core, title="Weak")
    red_team(core, weak["id"], revised_scores={k: 2 for k in ideas.SCORE_WEIGHTS})  # author said 4s; the red team says 2s
    r = await approve(weak["id"])
    assert not r.ok and ("launch bar" in r.content or "proposal.floor" in r.content)
    loss = submit(core, title="Loss maker")
    red_team(core, loss["id"], revised_projection={"time_to_first_revenue_days": 30, "month3_profit_usd": -50,
                                                   "month6_profit_usd": -10})
    r = await approve(loss["id"])
    assert not r.ok and "month 6" in r.content
    good = submit(core, title="Good")
    red_team(core, good["id"])
    r = await approve(good["id"])
    assert r.ok, r.content
    oid = core.db.scalar("SELECT org_id FROM proposals WHERE id = ?", (good["id"],))
    brief = core.db.scalar("SELECT brief FROM orgs WHERE id = ?", (oid,))
    assert "Business case:" in brief and "Red team review" in brief and "First customers:" in brief
    goals = {g["title"]: g for g in core.db.all("SELECT * FROM goals WHERE org_id = ?", (oid,))}
    assert goals["First paying customer"]["metric_key"] == "customers"
    assert goals["Revenue on plan (first 60 days)"]["target"] == 149 + 447


async def test_no_direct_create_business(core, org):
    r = await invoke(Ctx(core=core, agent=org["pceo"]), "create_business", {"name": "x", "template": "generic", "brief": "y" * 300})
    assert not r.ok


def test_owner_new_business_form_goes_through_the_pipeline(core, org):
    with TestClient(create_app(core, run_scheduler=False)) as c:
        with_csrf(c)
        before = core.db.scalar("SELECT COUNT(*) AS n FROM orgs")
        r = c.post("/new", data={"name": "Gutter Leads", "template": "rank-and-rent", "cap": "40",
                                 "brief": "Rank local gutter-cleaning sites and rent the leads to contractors."},
                   follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/ideas")
        assert core.db.scalar("SELECT COUNT(*) AS n FROM orgs") == before  # nothing launched without review
        t = core.db.one("SELECT * FROM tasks WHERE title LIKE 'Owner idea:%'")
        assert t["assignee_id"] == analyst(core)["id"]
        assert "source='owner'" in t["description"] and "Rank local gutter-cleaning" in t["description"]
        assert "rank-and-rent" in t["description"] and "$40" in t["description"]


async def test_dry_run_owner_idea_is_red_teamed_end_to_end(core, org):
    from bizops.scheduler import Scheduler
    core.settings.policy["platform"]["monthly_cap_usd"] = 1000
    ideas.owner_idea(core.db, core.settings, idea="AI receptionists for dentists in Columbus")
    core.db.execute("UPDATE agents SET min_interval_s = 0")
    await Scheduler(core, tick_s=0.05).drain(timeout=60)
    p = core.db.one("SELECT * FROM proposals")
    assert p["source"] == "owner" and p["redteam_verdict"] == "go"
    review = json.loads(p["redteam_json"])
    assert len(review["risks"]) >= 5 and len(review["counter_evidence"]) >= 2


def test_legacy_proposals_are_sent_to_the_red_team(core, org):
    p = submit(core)
    core.db.execute("UPDATE proposals SET status = 'new', redteam_verdict = NULL WHERE id = ?", (p["id"],))
    core.db.execute("UPDATE tasks SET status = 'done' WHERE title LIKE 'Red-team%'")
    assert ideas.red_team_backlog(core.db, core.settings) == 1
    assert core.db.scalar("SELECT status FROM proposals WHERE id = ?", (p["id"],)) == "red_team"
    assert ideas.red_team_backlog(core.db, core.settings) == 0  # one open task is enough


def test_existing_platform_gets_a_red_team(core):
    from bizops.seed import ensure_platform
    root = ensure_platform(core)
    core.db.execute("DELETE FROM tasks")
    core.db.execute("UPDATE agents SET status = 'retired' WHERE role = 'red_team'")
    ensure_platform(core)
    rt = core.db.one("SELECT * FROM agents WHERE role = 'red_team' AND status = 'active'")
    assert rt and rt["org_id"] == root and rt["parent_agent_id"] is None

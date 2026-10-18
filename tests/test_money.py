import pytest

from bizops import ledger
from bizops.engine import create_task
from bizops.gateway import invoke
from bizops.ledger import LedgerError
from bizops.payments import create_payee, deposit
from bizops.tools import Ctx


@pytest.fixture
def funded(core, org):
    deposit(core.db, org_id=org["org"], amount_usd=40)
    core.db.execute("UPDATE agents SET budget_micros = 100000000 WHERE id = ?", (org["ceo"]["id"],))
    pid = create_payee(core.db, name="Vendor", org_id=org["org"], max_per_payment_usd=20)
    task = create_task(core.db, creator=None, assignee_id=org["ceo"]["id"], title="buy", deliverable="receipt")
    return {"payee": pid, "task": task["id"]}


def pay(core, org, funded, amount, **extra):
    args = {"payee_id": funded["payee"], "amount_usd": amount, "memo": "domain", "task_id": funded["task"],
            "reason": "needed for the task", **extra}
    return invoke(Ctx(core=core, agent=org["ceo"]), "pay", args)


async def test_pay_happy_path(core, org, funded):
    r = await pay(core, org, funded, 9)
    assert r.ok, r.content
    assert ledger.wallet_balance(core.db, org["org"]) == 31_000_000
    assert ledger.spent(core.db, since="2000", agent_id=org["ceo"]["id"]) == 9_000_000
    d = core.db.one("SELECT * FROM policy_decisions WHERE action = 'pay'")
    assert d["verdict"] == "allowed" and "llm:allow" in d["approver"]


async def test_pay_idempotent(core, org, funded):
    assert (await pay(core, org, funded, 9, idempotency_key="k1")).ok
    r = await pay(core, org, funded, 9, idempotency_key="k1")
    assert r.ok and "duplicate" in r.content
    assert ledger.wallet_balance(core.db, org["org"]) == 31_000_000


async def test_pay_limits(core, org, funded):
    r = await pay(core, org, funded, 25)
    assert not r.ok and "pay.payee_max" in r.content
    r = await pay(core, org, funded, 9, payee_id="pay_nope")
    assert not r.ok and "pay.payee" in r.content
    core.settings.policy["platform"]["daily_cap_usd"] = 100
    for i in range(4):
        assert (await pay(core, org, funded, 10, idempotency_key=f"x{i}")).ok
    r = await pay(core, org, funded, 1, idempotency_key="x9")
    assert not r.ok and "pay.balance" in r.content


async def test_daily_max(core, org, funded):
    core.settings.policy["money"]["daily_payments_max_usd_per_org"] = 15
    assert (await pay(core, org, funded, 10, idempotency_key="a")).ok
    r = await pay(core, org, funded, 10, idempotency_key="b")
    assert not r.ok and "pay.daily_max" in r.content


async def test_approver_blocks(core, org, funded):
    r = await pay(core, org, funded, 5, memo="BLOCKME please")
    assert not r.ok and "approver" in r.content
    assert ledger.wallet_balance(core.db, org["org"]) == 40_000_000


async def test_rules_approver_needs_task(core, org, funded):
    r = await invoke(Ctx(core=core, agent=org["ceo"]), "pay",
                     {"payee_id": funded["payee"], "amount_usd": 5, "memo": "x", "reason": "r"})
    assert not r.ok and "approver (rules)" in r.content


async def test_agents_without_money_grant_cannot_pay(core, org, funded):
    r = await invoke(Ctx(core=core, agent=org["coo"]), "pay", {"payee_id": funded["payee"], "amount_usd": 1,
                                                               "memo": "x", "task_id": funded["task"]})
    assert not r.ok and "grant" in r.content


async def test_request_payee_is_hard_stop(core, org):
    r = await invoke(Ctx(core=core, agent=org["ceo"]), "request_payee", {"name": "Ads Inc", "details": "ads"})
    assert r.outcome == "hard_stop"


def test_ledger_source_rules(core, org):
    with pytest.raises(LedgerError):
        ledger.record(core.db, org_id=org["org"], kind="revenue", amount_micros=1, source="tool")
    with pytest.raises(LedgerError):
        ledger.record(core.db, org_id=org["org"], kind="deposit", amount_micros=1, source="provider")
    with pytest.raises(PermissionError):
        create_payee(core.db, name="x", org_id=None, created_by="agent")


async def test_failed_provider_reverses(core, org, funded):
    class Broken:
        name = "broken"

        async def send(self, **kw):
            raise RuntimeError("bank down")
    core.payments = Broken()
    r = await pay(core, org, funded, 9)
    assert r.outcome == "error"
    assert ledger.wallet_balance(core.db, org["org"]) == 40_000_000

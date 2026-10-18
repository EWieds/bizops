from bizops.gateway import invoke
from bizops.tools import Ctx


async def test_auditor_reports_to_owner_and_records_findings(core, org):
    auditor = core.db.one("SELECT * FROM agents WHERE role = 'auditor'")
    assert auditor["parent_agent_id"] is None and auditor["org_id"] == org["root"]
    r = await invoke(Ctx(core=core, agent=auditor), "audit_evidence", {"org_id": org["org"], "days": 7})
    assert r.ok and "spend_by_agent" in r.content
    r = await invoke(Ctx(core=core, agent=auditor), "record_finding",
                     {"org_id": org["org"], "kind": "cost_anomaly", "severity": "medium", "summary": "s", "evidence": " "})
    assert not r.ok and "evidence" in r.content
    r = await invoke(Ctx(core=core, agent=auditor), "record_finding",
                     {"org_id": org["org"], "kind": "cost_anomaly", "severity": "medium", "summary": "s", "evidence": "ledger rows"})
    assert r.ok


async def test_team_cannot_audit(core, org):
    r = await invoke(Ctx(core=core, agent=org["ceo"]), "record_finding",
                     {"kind": "other", "severity": "low", "summary": "s", "evidence": "e"})
    assert not r.ok and "grant" in r.content

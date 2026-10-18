"""Seed data: the platform root org plus one fake business, so `serve --dry-run` shows life."""

from __future__ import annotations

from .core import Core
from .engine import create_org_from_template, create_task, human_message, org_ceo, root_org
from .payments import create_payee, deposit

DEMO_BRIEF = """Rank-and-rent lead gen for lawn care in Columbus, OH. Build a simple site that ranks
for 'lawn care columbus', capture leads, and rent the leads to a local lawn company for a flat
monthly fee. Keep spend tiny until we see real leads."""


def ensure_platform(core: Core) -> str:
    root = root_org(core.db)
    if root:
        return root["id"]
    return create_org_from_template(core.db, core.settings, template="platform", created_by="system")


def seed_demo(core: Core) -> dict[str, str]:
    db, settings = core.db, core.settings
    root_id = ensure_platform(core)
    existing = db.one("SELECT id FROM orgs WHERE slug = 'acme-lawn-leads'")
    if existing:
        return {"platform": root_id, "demo": existing["id"]}
    pceo = org_ceo(db, root_id)
    org_id = create_org_from_template(db, settings, template="generic", name="Acme Lawn Leads", brief=DEMO_BRIEF,
                                      parent_org_id=root_id, supervisor_agent_id=pceo["id"], created_by="human")
    ceo = org_ceo(db, org_id)
    deposit(db, org_id=org_id, amount_usd=40, memo="seed: starting wallet")
    create_payee(db, name="Namecheap (sandbox)", org_id=org_id, kind="vendor", max_per_payment_usd=20,
                 notes="domains only")
    create_task(db, creator=None, assignee_id=ceo["id"], title="Register domain acmelawnleads.com",
                deliverable="A payment receipt in the ledger and the domain noted in the KB")
    create_task(db, creator=None, assignee_id=ceo["id"], title="Sign the lead-buyer contract with Green Lawns LLC",
                deliverable="Signed contract on file")
    human_message(db, settings, ceo["id"], "Welcome aboard. Research the Columbus lawn-care market and plan the "
                                           "first 30 days. Keep it cheap.", type="instruction")
    return {"platform": root_id, "demo": org_id}

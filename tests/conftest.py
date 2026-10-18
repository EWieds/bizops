import pytest

from bizops.config import Settings
from bizops.core import Core
from bizops.engine import create_org_from_template, org_ceo
from bizops.seed import ensure_platform


@pytest.fixture(autouse=True)
def clean_env():
    """write_env() also sets os.environ; keep tests independent."""
    import os
    before = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(before)


@pytest.fixture
def settings(tmp_path):
    return Settings.load(home=tmp_path, db_path=tmp_path / "t.db", dry_run=True,
                         policy={"sandbox": {"reply_delay_s": 0},
                                 "heartbeats": {"maintenance_every_s": 0, "inbound_every_s": 0}})


@pytest.fixture
def core(settings):
    c = Core(settings).init()
    yield c


def fast(core):
    """No min interval between runs, so tests don't sleep."""
    core.db.execute("UPDATE agents SET min_interval_s = 0")


@pytest.fixture
def org(core):
    """Platform + one generic business. Returns dict of ids."""
    root = ensure_platform(core)
    pceo = org_ceo(core.db, root)
    oid = create_org_from_template(core.db, core.settings, template="classic", name="Test Co",
                                   parent_org_id=root, supervisor_agent_id=pceo["id"])
    fast(core)
    agents = {a["role"]: a for a in core.db.all("SELECT * FROM agents WHERE org_id = ?", (oid,))}
    return {"root": root, "org": oid, "pceo": pceo, **agents}

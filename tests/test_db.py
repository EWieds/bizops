from importlib import resources

from bizops.db import Database


def test_migrate_idempotent_and_wal(tmp_path):
    db = Database(tmp_path / "x.db")
    assert db.migrate() == ["0001_init", "0002_goals_audit", "0003_business", "0004_ideas_chat",
                            "0005_money_safety", "0005_purchases_browser", "0006_proposal_case",
                            "0007_idea_sources"]
    assert db.migrate() == []
    assert db.scalar("PRAGMA journal_mode") == "wal"


def test_schema_is_portable():
    files = [f for f in resources.files("bizops.migrations").iterdir() if f.name.endswith(".sql")]
    sql = "\n".join(f.read_text() for f in files).upper()
    for bad in ("AUTOINCREMENT", "WITHOUT ROWID", "STRICT", "DATETIME(", "JSON_"):
        assert bad not in sql


def test_nothing_mutates_events_or_ledger():
    """Append-only: no code anywhere updates or deletes audit events or ledger entries."""
    import re
    from pathlib import Path

    import bizops
    pat = re.compile(r"(UPDATE|DELETE\s+FROM)\s+(events|ledger_entries)\b", re.I)
    for f in Path(bizops.__file__).parent.rglob("*.py"):
        assert not pat.search(f.read_text()), f

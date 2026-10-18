-- Goals (OKR-style), auditor findings, and bookkeeping for heartbeats.

ALTER TABLE agents ADD COLUMN title TEXT NOT NULL DEFAULT '';
ALTER TABLE agents ADD COLUMN last_heartbeat_skip_at TEXT;
ALTER TABLE tasks ADD COLUMN last_nudged_at TEXT;
ALTER TABLE tasks ADD COLUMN nudges INTEGER NOT NULL DEFAULT 0;

-- A measurable goal owned by one agent. Org goals are the CEO's goals; reports' goals cascade from
-- them via parent_goal_id. Agents set status and progress notes; measured_value is written only by a
-- connector or the owner (agents can't fabricate progress numbers).
CREATE TABLE goals (
  id               TEXT PRIMARY KEY,
  org_id           TEXT NOT NULL REFERENCES orgs(id),
  owner_agent_id   TEXT NOT NULL REFERENCES agents(id),
  parent_goal_id   TEXT REFERENCES goals(id),
  title            TEXT NOT NULL,
  metric           TEXT NOT NULL,
  target           REAL,
  unit             TEXT NOT NULL DEFAULT '',
  measured_value   REAL,
  measured_source  TEXT,
  measured_at      TEXT,
  due_at           TEXT NOT NULL,
  review_every_s   INTEGER NOT NULL DEFAULT 259200,
  next_review_at   TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'active',
  progress_note    TEXT NOT NULL DEFAULT '',
  created_by       TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE INDEX ix_goals_org ON goals(org_id, status);
CREATE INDEX ix_goals_owner ON goals(owner_agent_id, status);
CREATE INDEX ix_goals_review ON goals(next_review_at);

-- Outside auditor findings (from the auditor agent or the code-level checks).
CREATE TABLE audit_findings (
  id               TEXT PRIMARY KEY,
  ts               TEXT NOT NULL,
  org_id           TEXT REFERENCES orgs(id),
  agent_id         TEXT REFERENCES agents(id),
  source           TEXT NOT NULL,
  kind             TEXT NOT NULL,
  severity         TEXT NOT NULL,
  summary          TEXT NOT NULL,
  evidence         TEXT NOT NULL DEFAULT '',
  recommendation   TEXT NOT NULL DEFAULT '',
  status           TEXT NOT NULL DEFAULT 'open',
  dedupe_key       TEXT,
  resolved_at      TEXT
);
CREATE INDEX ix_findings_status ON audit_findings(status, ts);
CREATE INDEX ix_findings_dedupe ON audit_findings(dedupe_key);

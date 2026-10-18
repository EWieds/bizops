-- Ideas pipeline (scored proposals) and chat attachments.

CREATE TABLE proposals (
  id               TEXT PRIMARY KEY,
  title            TEXT NOT NULL,
  summary          TEXT NOT NULL,
  thesis           TEXT NOT NULL,
  target_customer  TEXT NOT NULL,
  offer            TEXT NOT NULL,
  evidence_json    TEXT NOT NULL DEFAULT '[]',
  plan             TEXT NOT NULL DEFAULT '',
  starting_budget_usd REAL NOT NULL DEFAULT 50,
  kill_criteria    TEXT NOT NULL DEFAULT '',
  template         TEXT NOT NULL DEFAULT 'generic',
  scores_json      TEXT NOT NULL DEFAULT '{}',
  total_score      REAL NOT NULL DEFAULT 0,
  source           TEXT NOT NULL DEFAULT 'research',
  source_ref       TEXT,
  status           TEXT NOT NULL DEFAULT 'new',
  org_id           TEXT REFERENCES orgs(id),
  artifact_id      TEXT,
  created_by       TEXT NOT NULL,
  decided_by       TEXT,
  decision_reason  TEXT,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE INDEX ix_proposals_status ON proposals(status, total_score);

ALTER TABLE messages ADD COLUMN attachments_json TEXT;

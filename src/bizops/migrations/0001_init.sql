-- bizops schema v1 (M1 + safety core). Portable SQL: TEXT ids, ISO-8601 UTC TEXT
-- timestamps, INTEGER micro-dollars, JSON as TEXT. No SQLite-only types.

CREATE TABLE settings (
  key              TEXT PRIMARY KEY,
  value            TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);

CREATE TABLE orgs (
  id               TEXT PRIMARY KEY,
  parent_org_id    TEXT REFERENCES orgs(id),
  name             TEXT NOT NULL,
  slug             TEXT NOT NULL UNIQUE,
  template         TEXT NOT NULL,
  brief            TEXT NOT NULL DEFAULT '',
  status           TEXT NOT NULL DEFAULT 'active',
  monthly_cap_micros INTEGER NOT NULL DEFAULT 0,
  settings_json    TEXT NOT NULL DEFAULT '{}',
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);

CREATE TABLE agents (
  id               TEXT PRIMARY KEY,
  org_id           TEXT NOT NULL REFERENCES orgs(id),
  parent_agent_id  TEXT REFERENCES agents(id),
  role             TEXT NOT NULL,
  name             TEXT NOT NULL,
  system_prompt    TEXT NOT NULL,
  model_tier       TEXT NOT NULL DEFAULT 'worker',
  model_override   TEXT,
  grants_json      TEXT NOT NULL DEFAULT '[]',
  kpi_json         TEXT NOT NULL DEFAULT '{}',
  budget_micros    INTEGER NOT NULL DEFAULT 0,
  depth            INTEGER NOT NULL DEFAULT 0,
  status           TEXT NOT NULL DEFAULT 'active',
  wake_interval_s  INTEGER,
  min_interval_s   INTEGER NOT NULL DEFAULT 30,
  next_wake_at     TEXT,
  wake_requested_at TEXT,
  last_run_at      TEXT,
  created_by       TEXT,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE INDEX ix_agents_org ON agents(org_id);
CREATE INDEX ix_agents_parent ON agents(parent_agent_id);

CREATE TABLE tasks (
  id               TEXT PRIMARY KEY,
  org_id           TEXT NOT NULL REFERENCES orgs(id),
  assignee_id      TEXT NOT NULL REFERENCES agents(id),
  created_by       TEXT NOT NULL,
  parent_task_id   TEXT REFERENCES tasks(id),
  title            TEXT NOT NULL,
  description      TEXT NOT NULL DEFAULT '',
  deliverable      TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'open',
  attempts         INTEGER NOT NULL DEFAULT 0,
  max_attempts     INTEGER NOT NULL DEFAULT 3,
  result           TEXT,
  blocked_reason   TEXT,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE INDEX ix_tasks_assignee ON tasks(assignee_id, status);
CREATE INDEX ix_tasks_org ON tasks(org_id, status);

CREATE TABLE messages (
  id               TEXT PRIMARY KEY,
  org_id           TEXT NOT NULL REFERENCES orgs(id),
  sender_kind      TEXT NOT NULL,
  sender_id        TEXT,
  recipient_kind   TEXT NOT NULL,
  recipient_id     TEXT,
  type             TEXT NOT NULL,
  body             TEXT NOT NULL,
  thread_id        TEXT NOT NULL,
  in_reply_to      TEXT REFERENCES messages(id),
  hop              INTEGER NOT NULL DEFAULT 0,
  expires_at       TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'unread',
  created_at       TEXT NOT NULL,
  read_at          TEXT
);
CREATE INDEX ix_messages_inbox ON messages(recipient_kind, recipient_id, status);
CREATE INDEX ix_messages_thread ON messages(thread_id);

CREATE TABLE runs (
  id               TEXT PRIMARY KEY,
  agent_id         TEXT NOT NULL REFERENCES agents(id),
  org_id           TEXT NOT NULL REFERENCES orgs(id),
  wake_reason      TEXT NOT NULL,
  model            TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'running',
  turns            INTEGER NOT NULL DEFAULT 0,
  input_tokens     INTEGER NOT NULL DEFAULT 0,
  output_tokens    INTEGER NOT NULL DEFAULT 0,
  cost_micros      INTEGER NOT NULL DEFAULT 0,
  summary          TEXT,
  error            TEXT,
  started_at       TEXT NOT NULL,
  ended_at         TEXT
);
CREATE INDEX ix_runs_agent ON runs(agent_id, started_at);

-- Append-only. The gateway writes a 'before' row, then an 'after' row (same call_id).
CREATE TABLE events (
  id               TEXT PRIMARY KEY,
  ts               TEXT NOT NULL,
  org_id           TEXT REFERENCES orgs(id),
  agent_id         TEXT REFERENCES agents(id),
  run_id           TEXT REFERENCES runs(id),
  actor_kind       TEXT NOT NULL,
  kind             TEXT NOT NULL,
  action           TEXT NOT NULL,
  phase            TEXT NOT NULL,
  call_id          TEXT,
  reason           TEXT,
  args_json        TEXT,
  result_json      TEXT,
  outcome          TEXT,
  cost_micros      INTEGER NOT NULL DEFAULT 0,
  ref_type         TEXT,
  ref_id           TEXT
);
CREATE INDEX ix_events_ts ON events(ts);
CREATE INDEX ix_events_org_ts ON events(org_id, ts);
CREATE INDEX ix_events_agent_ts ON events(agent_id, ts);
CREATE INDEX ix_events_call ON events(call_id);

-- Append-only money ledger.
--   model_cost / spend : count against budgets and caps
--   deposit            : owner funds an org wallet (source = human only)
--   payment            : money sent from an org wallet to an allowlisted payee (also a spend)
--   revenue            : connector or human only
CREATE TABLE ledger_entries (
  id               TEXT PRIMARY KEY,
  ts               TEXT NOT NULL,
  org_id           TEXT NOT NULL REFERENCES orgs(id),
  agent_id         TEXT REFERENCES agents(id),
  run_id           TEXT REFERENCES runs(id),
  kind             TEXT NOT NULL,
  amount_micros    INTEGER NOT NULL,
  source           TEXT NOT NULL,
  memo             TEXT,
  ref_id           TEXT,
  event_id         TEXT
);
CREATE INDEX ix_ledger_org_ts ON ledger_entries(org_id, ts);
CREATE INDEX ix_ledger_agent_ts ON ledger_entries(agent_id, ts);

-- Every gated action: verdict, the rule that decided it, the approver's reasoning.
-- verdict = allowed | blocked | hard_stop. Hard stops with status 'open' are the
-- "needs a human" queue; blocked rows are the "blocked actions" list.
CREATE TABLE policy_decisions (
  id               TEXT PRIMARY KEY,
  ts               TEXT NOT NULL,
  org_id           TEXT REFERENCES orgs(id),
  agent_id         TEXT REFERENCES agents(id),
  run_id           TEXT REFERENCES runs(id),
  call_id          TEXT,
  action           TEXT NOT NULL,
  args_json        TEXT,
  verdict          TEXT NOT NULL,
  rule             TEXT NOT NULL,
  approver         TEXT,
  approver_reason  TEXT,
  confidence       REAL,
  task_id          TEXT REFERENCES tasks(id),
  status           TEXT NOT NULL DEFAULT 'closed',
  resolved_by      TEXT,
  resolution       TEXT,
  resolved_at      TEXT
);
CREATE INDEX ix_policy_verdict ON policy_decisions(verdict, status);

-- Allowlisted payees. Only a human creates or enables one; agents can only request.
CREATE TABLE payees (
  id               TEXT PRIMARY KEY,
  org_id           TEXT REFERENCES orgs(id),
  name             TEXT NOT NULL,
  kind             TEXT NOT NULL DEFAULT 'vendor',
  provider_ref     TEXT NOT NULL DEFAULT '',
  max_per_payment_micros INTEGER NOT NULL DEFAULT 0,
  status           TEXT NOT NULL DEFAULT 'active',
  notes            TEXT NOT NULL DEFAULT '',
  created_by       TEXT NOT NULL,
  created_at       TEXT NOT NULL
);

CREATE TABLE memory_notes (
  id               TEXT PRIMARY KEY,
  org_id           TEXT NOT NULL REFERENCES orgs(id),
  agent_id         TEXT REFERENCES agents(id),
  key              TEXT NOT NULL,
  body             TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE INDEX ix_memory_owner ON memory_notes(org_id, agent_id);

CREATE TABLE artifacts (
  id               TEXT PRIMARY KEY,
  org_id           TEXT NOT NULL REFERENCES orgs(id),
  agent_id         TEXT REFERENCES agents(id),
  task_id          TEXT REFERENCES tasks(id),
  name             TEXT NOT NULL,
  path             TEXT NOT NULL,
  mime             TEXT NOT NULL,
  size_bytes       INTEGER NOT NULL,
  sha256           TEXT NOT NULL,
  created_at       TEXT NOT NULL
);

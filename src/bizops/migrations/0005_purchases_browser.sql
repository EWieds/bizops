-- Purchases (agent asks, the CIO decides, a vendor-locked virtual card pays) and the agent browser.

CREATE TABLE purchases (
  id               TEXT PRIMARY KEY,
  org_id           TEXT NOT NULL REFERENCES orgs(id),
  agent_id         TEXT NOT NULL REFERENCES agents(id),
  task_id          TEXT,
  vendor           TEXT NOT NULL,
  vendor_url       TEXT NOT NULL DEFAULT '',
  item             TEXT NOT NULL,
  amount_micros    INTEGER NOT NULL,
  period           TEXT NOT NULL,              -- once | monthly
  reason           TEXT NOT NULL,
  status           TEXT NOT NULL,              -- pending | approved | paused | denied | cancelled | failed
  decided_by       TEXT,                       -- agent id of the CIO, or 'human'
  decision_reason  TEXT,
  decided_at       TEXT,
  card_provider    TEXT,
  card_token       TEXT,
  card_last4       TEXT,
  next_charge_at   TEXT,                       -- monthly: when the wallet is debited again
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE INDEX ix_purchases_org ON purchases(org_id, status);
CREATE INDEX ix_purchases_status ON purchases(status, next_charge_at);

-- Logins agents create for websites. Agents only ever see the name, the browser fills the values.
CREATE TABLE credentials (
  id               TEXT PRIMARY KEY,
  org_id           TEXT NOT NULL REFERENCES orgs(id),
  name             TEXT NOT NULL,
  site             TEXT NOT NULL DEFAULT '',
  username         TEXT NOT NULL DEFAULT '',
  secret           TEXT NOT NULL,
  created_by       TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  UNIQUE (org_id, name)
);
-- Agents that already exist get the new grants in core.upgrade_grants (portable SQL has no JSON functions).

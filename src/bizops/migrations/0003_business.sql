-- The money-making loop: leads -> email outreach -> offers/payment links -> payments -> customers ->
-- fulfillment (voice agents, sites). Real providers or the sandbox write the same tables.

ALTER TABLE goals ADD COLUMN metric_key TEXT;

CREATE TABLE leads (
  id               TEXT PRIMARY KEY,
  org_id           TEXT NOT NULL REFERENCES orgs(id),
  company          TEXT NOT NULL,
  contact_name     TEXT NOT NULL DEFAULT '',
  email            TEXT,
  phone            TEXT,
  website          TEXT,
  city             TEXT NOT NULL DEFAULT '',
  industry         TEXT NOT NULL DEFAULT '',
  source_url       TEXT NOT NULL DEFAULT '',
  notes            TEXT NOT NULL DEFAULT '',
  stage            TEXT NOT NULL DEFAULT 'new',
  owner_agent_id   TEXT REFERENCES agents(id),
  emails_sent      INTEGER NOT NULL DEFAULT 0,
  last_contacted_at TEXT,
  last_reply_at    TEXT,
  next_followup_at TEXT,
  sandbox          INTEGER NOT NULL DEFAULT 0,
  sim_json         TEXT,
  created_by       TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE INDEX ix_leads_org ON leads(org_id, stage);
CREATE INDEX ix_leads_email ON leads(email);

CREATE TABLE emails (
  id               TEXT PRIMARY KEY,
  org_id           TEXT NOT NULL REFERENCES orgs(id),
  lead_id          TEXT REFERENCES leads(id),
  agent_id         TEXT REFERENCES agents(id),
  direction        TEXT NOT NULL,
  from_addr        TEXT NOT NULL,
  to_addr          TEXT NOT NULL,
  subject          TEXT NOT NULL,
  body             TEXT NOT NULL,
  status           TEXT NOT NULL,
  provider         TEXT NOT NULL,
  provider_id      TEXT,
  error            TEXT,
  processed        INTEGER NOT NULL DEFAULT 0,
  sim_due_at       TEXT,
  created_at       TEXT NOT NULL
);
CREATE INDEX ix_emails_lead ON emails(lead_id, created_at);
CREATE INDEX ix_emails_inbound ON emails(direction, processed);

CREATE TABLE suppressions (
  email            TEXT PRIMARY KEY,
  reason           TEXT NOT NULL,
  created_at       TEXT NOT NULL
);

CREATE TABLE offers (
  id               TEXT PRIMARY KEY,
  org_id           TEXT NOT NULL REFERENCES orgs(id),
  name             TEXT NOT NULL,
  description      TEXT NOT NULL DEFAULT '',
  price_cents      INTEGER NOT NULL,
  currency         TEXT NOT NULL DEFAULT 'usd',
  interval         TEXT NOT NULL DEFAULT 'one_time',
  provider         TEXT NOT NULL,
  provider_product_id TEXT,
  provider_price_id TEXT,
  provider_link_id TEXT,
  payment_url      TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'active',
  created_by       TEXT NOT NULL,
  created_at       TEXT NOT NULL
);
CREATE INDEX ix_offers_org ON offers(org_id);

CREATE TABLE customers (
  id               TEXT PRIMARY KEY,
  org_id           TEXT NOT NULL REFERENCES orgs(id),
  lead_id          TEXT REFERENCES leads(id),
  company          TEXT NOT NULL,
  email            TEXT,
  status           TEXT NOT NULL DEFAULT 'onboarding',
  offer_id         TEXT REFERENCES offers(id),
  mrr_cents        INTEGER NOT NULL DEFAULT 0,
  voice_agent_id   TEXT,
  sandbox          INTEGER NOT NULL DEFAULT 0,
  notes            TEXT NOT NULL DEFAULT '',
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE INDEX ix_customers_org ON customers(org_id, status);

CREATE TABLE payments_in (
  id               TEXT PRIMARY KEY,
  org_id           TEXT NOT NULL REFERENCES orgs(id),
  offer_id         TEXT REFERENCES offers(id),
  lead_id          TEXT REFERENCES leads(id),
  customer_id      TEXT REFERENCES customers(id),
  provider         TEXT NOT NULL,
  provider_ref     TEXT NOT NULL UNIQUE,
  amount_cents     INTEGER NOT NULL,
  currency         TEXT NOT NULL DEFAULT 'usd',
  payer_email      TEXT,
  status           TEXT NOT NULL DEFAULT 'paid',
  sandbox          INTEGER NOT NULL DEFAULT 0,
  paid_at          TEXT NOT NULL,
  clears_at        TEXT NOT NULL
);
CREATE INDEX ix_payments_in_org ON payments_in(org_id, paid_at);

CREATE TABLE voice_agents (
  id               TEXT PRIMARY KEY,
  org_id           TEXT NOT NULL REFERENCES orgs(id),
  customer_id      TEXT REFERENCES customers(id),
  name             TEXT NOT NULL,
  business_name    TEXT NOT NULL,
  config_json      TEXT NOT NULL,
  provider         TEXT NOT NULL,
  provider_ref     TEXT,
  phone_number     TEXT,
  status           TEXT NOT NULL DEFAULT 'demo',
  created_by       TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE INDEX ix_voice_org ON voice_agents(org_id);

CREATE TABLE sites (
  id               TEXT PRIMARY KEY,
  org_id           TEXT NOT NULL REFERENCES orgs(id),
  path             TEXT NOT NULL,
  title            TEXT NOT NULL,
  html             TEXT NOT NULL,
  created_by       TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_sites_path ON sites(org_id, path);

-- Sandbox market: scheduled reactions of simulated prospects (replies, payments). Never used for real leads.
CREATE TABLE sim_actions (
  id               TEXT PRIMARY KEY,
  due_at           TEXT NOT NULL,
  kind             TEXT NOT NULL,
  payload_json     TEXT NOT NULL,
  done             INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX ix_sim_due ON sim_actions(done, due_at);

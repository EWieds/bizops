-- Refunds, disputes and renewals, revocable owner sessions and login lockout, the capital-allocation waterfall.

ALTER TABLE payments_in ADD COLUMN refunded_cents INTEGER NOT NULL DEFAULT 0;
ALTER TABLE payments_in ADD COLUMN disputed_cents INTEGER NOT NULL DEFAULT 0;
ALTER TABLE payments_in ADD COLUMN provider_payment_intent TEXT;
ALTER TABLE payments_in ADD COLUMN provider_invoice TEXT;
ALTER TABLE payments_in ADD COLUMN provider_charge TEXT;
ALTER TABLE payments_in ADD COLUMN provider_subscription TEXT;
CREATE INDEX ix_payments_in_pi ON payments_in(provider_payment_intent);
CREATE INDEX ix_payments_in_invoice ON payments_in(provider_invoice);
CREATE INDEX ix_payments_in_sub ON payments_in(provider_subscription);

-- Owner login sessions (server-side so they can be revoked) and failed-login lockout.
CREATE TABLE sessions (
  id               TEXT PRIMARY KEY,          -- sha256 of the cookie token (the token itself is never stored)
  csrf             TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  expires_at       TEXT NOT NULL,
  last_seen_at     TEXT NOT NULL,
  ip               TEXT,
  user_agent       TEXT,
  revoked_at       TEXT
);
CREATE TABLE login_attempts (
  id               TEXT PRIMARY KEY,
  ts               TEXT NOT NULL,
  ip               TEXT NOT NULL,
  ok               INTEGER NOT NULL
);
CREATE INDEX ix_login_attempts ON login_attempts(ip, ts);

-- Capital allocation: one row per org per month (the computed waterfall), and two-key allocation decisions.
CREATE TABLE allocation_periods (
  id               TEXT PRIMARY KEY,
  month            TEXT NOT NULL,             -- YYYY-MM (a complete month)
  org_id           TEXT NOT NULL REFERENCES orgs(id),
  cleared_revenue_micros INTEGER NOT NULL,
  costs_micros     INTEGER NOT NULL,
  verified_profit_micros INTEGER NOT NULL,
  tax_reserve_micros INTEGER NOT NULL,
  operating_reserve_micros INTEGER NOT NULL,
  surplus_micros   INTEGER NOT NULL,
  status           TEXT NOT NULL DEFAULT 'open', -- open | proposed | decided
  created_at       TEXT NOT NULL,
  UNIQUE (month, org_id)
);
CREATE TABLE allocations (
  id               TEXT PRIMARY KEY,
  period_id        TEXT REFERENCES allocation_periods(id),
  month            TEXT NOT NULL,
  scope            TEXT NOT NULL,             -- org (split own surplus) | treasury (move treasury money)
  from_org_id      TEXT REFERENCES orgs(id),  -- org surplus source (NULL = platform treasury)
  to_org_id        TEXT REFERENCES orgs(id),  -- recipient org (NULL = platform treasury)
  amount_micros    INTEGER NOT NULL,
  reason           TEXT NOT NULL,
  numbers_json     TEXT NOT NULL DEFAULT '{}',
  expected_result  TEXT NOT NULL DEFAULT '',
  proposed_by      TEXT NOT NULL,             -- agent id (CEO key)
  reviewed_by      TEXT,                      -- agent id (CFO key)
  cfo_score        REAL,
  cfo_reason       TEXT,
  status           TEXT NOT NULL DEFAULT 'proposed', -- proposed | executed | rejected | escalated
  executed_at      TEXT,
  actual_result    TEXT,
  outcome_score    REAL,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE INDEX ix_allocations_month ON allocations(month, status);

-- Proposals carry a full business case (pricing, acquisition, marketing, costs, unit economics, projections)
-- and an independent red-team review that must happen before anything can launch.

ALTER TABLE proposals ADD COLUMN business_case_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE proposals ADD COLUMN checks_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE proposals ADD COLUMN review_round INTEGER NOT NULL DEFAULT 0;
ALTER TABLE proposals ADD COLUMN redteam_json TEXT;
ALTER TABLE proposals ADD COLUMN redteam_verdict TEXT;
ALTER TABLE proposals ADD COLUMN redteam_score REAL;
ALTER TABLE proposals ADD COLUMN redteam_by TEXT;
ALTER TABLE proposals ADD COLUMN redteam_at TEXT;

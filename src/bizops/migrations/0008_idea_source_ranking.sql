-- Channels are researched in full, most viewed first by default (or newest first), optionally only the top N.

ALTER TABLE idea_sources ADD COLUMN order_by TEXT NOT NULL DEFAULT 'views';
ALTER TABLE idea_sources ADD COLUMN max_videos INTEGER NOT NULL DEFAULT 0;
ALTER TABLE idea_sources ADD COLUMN total_videos INTEGER;
ALTER TABLE idea_sources ADD COLUMN listing TEXT;
ALTER TABLE idea_source_items ADD COLUMN view_count INTEGER;
ALTER TABLE idea_source_items ADD COLUMN duration_s INTEGER;
ALTER TABLE idea_source_items ADD COLUMN list_rank INTEGER;
ALTER TABLE idea_source_items ADD COLUMN is_new INTEGER NOT NULL DEFAULT 0;
UPDATE idea_source_items SET status = 'new' WHERE status = 'skipped' AND error IS NULL;

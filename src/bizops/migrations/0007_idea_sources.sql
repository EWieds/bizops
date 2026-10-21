-- Idea sources the owner subscribes to (YouTube channels): new videos become proposal research for the analyst.

CREATE TABLE idea_sources (
  id               TEXT PRIMARY KEY,
  kind             TEXT NOT NULL,              -- youtube_channel
  url              TEXT NOT NULL,
  channel_id       TEXT NOT NULL,
  title            TEXT NOT NULL DEFAULT '',
  status           TEXT NOT NULL DEFAULT 'active',   -- active | paused
  last_checked_at  TEXT,
  last_error       TEXT,
  created_at       TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_idea_sources ON idea_sources(kind, channel_id);

CREATE TABLE idea_source_items (
  id               TEXT PRIMARY KEY,
  source_id        TEXT NOT NULL REFERENCES idea_sources(id),
  video_id         TEXT NOT NULL,
  title            TEXT NOT NULL DEFAULT '',
  published_at     TEXT,
  status           TEXT NOT NULL,              -- new | sent | skipped | failed
  artifact_id      TEXT,
  task_id          TEXT,
  error            TEXT,
  created_at       TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_idea_source_items ON idea_source_items(source_id, video_id);

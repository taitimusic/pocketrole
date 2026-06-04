-- ============================================================
-- Migration 004: Story Memory (v2 upgrade Phase A)
-- ============================================================

CREATE TABLE IF NOT EXISTS story_memory (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id        TEXT    NOT NULL,
    memory_type     TEXT    NOT NULL,
    summary         TEXT    NOT NULL,
    involved_chars  JSON,
    trigger_turn    INTEGER NOT NULL,
    importance      REAL    DEFAULT 0.5,
    emotional_tone  TEXT,
    is_active       INTEGER DEFAULT 1,
    created_at      TEXT    DEFAULT (datetime('now')),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_story_memory_story
    ON story_memory(story_id, importance DESC);

CREATE INDEX IF NOT EXISTS idx_story_memory_active
    ON story_memory(story_id, is_active);

INSERT OR IGNORE INTO schema_version (version, description)
VALUES (4, 'story_memory table added');

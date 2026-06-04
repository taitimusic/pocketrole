BEGIN;

CREATE TABLE IF NOT EXISTS story_utterance_settings (
    story_id   TEXT PRIMARY KEY,
    max_chars  INTEGER NOT NULL DEFAULT 180,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE
);

INSERT OR IGNORE INTO schema_version (version, description)
VALUES (31, 'story-level utterance character limit settings');

COMMIT;

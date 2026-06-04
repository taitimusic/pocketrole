BEGIN;

CREATE TABLE IF NOT EXISTS story_episodes (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id            TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'active',
    episode_type        TEXT NOT NULL,
    goal                TEXT NOT NULL,
    stakes              TEXT,
    active_pattern_id   INTEGER,
    active_pattern_type TEXT,
    focus_char_ids      JSON NOT NULL DEFAULT '[]',
    carry_over_hook_ids JSON NOT NULL DEFAULT '[]',
    focus_place_id      TEXT,
    opened_turn         INTEGER NOT NULL,
    last_progress_turn  INTEGER,
    closed_turn         INTEGER,
    exit_condition      TEXT,
    summary             TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE,
    FOREIGN KEY (active_pattern_id) REFERENCES story_interaction_patterns(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_story_episodes_story_status
    ON story_episodes(story_id, status);

CREATE INDEX IF NOT EXISTS idx_story_episodes_story_opened_turn
    ON story_episodes(story_id, opened_turn DESC);

INSERT OR IGNORE INTO schema_version (version, description)
VALUES (15, 'story_episodes table added');

COMMIT;

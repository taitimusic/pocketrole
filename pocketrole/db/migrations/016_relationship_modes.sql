CREATE TABLE IF NOT EXISTS relationship_modes (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id              TEXT    NOT NULL,
    char_id_from          TEXT    NOT NULL,
    char_id_to            TEXT    NOT NULL,
    mode_type             TEXT    NOT NULL,
    status                TEXT    NOT NULL DEFAULT 'active',
    summary               TEXT,
    confidence            REAL    NOT NULL DEFAULT 0.5,
    intensity             REAL    NOT NULL DEFAULT 0.5,
    source_pattern_id     INTEGER,
    source_episode_id     INTEGER,
    first_detected_turn   INTEGER NOT NULL,
    last_reinforced_turn  INTEGER NOT NULL,
    last_trigger_event_id INTEGER,
    created_at            TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at            TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_relationship_modes_active_pair
    ON relationship_modes(story_id, char_id_from, char_id_to)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_relationship_modes_story_status_type
    ON relationship_modes(story_id, status, mode_type);

CREATE INDEX IF NOT EXISTS idx_relationship_modes_story_pair_status
    ON relationship_modes(story_id, char_id_from, char_id_to, status);

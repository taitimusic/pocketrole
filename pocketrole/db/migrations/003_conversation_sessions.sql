CREATE TABLE IF NOT EXISTS conversation_sessions (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id                   TEXT    NOT NULL,
    place_id                   TEXT    NOT NULL,
    participant_ids            JSON    NOT NULL DEFAULT '[]',
    status                     TEXT    NOT NULL DEFAULT 'active',
    last_speaker_id            TEXT,
    last_log_id                INTEGER,
    started_at_sim_datetime    TEXT    NOT NULL,
    last_activity_sim_datetime TEXT    NOT NULL,
    last_turn_number           INTEGER,
    created_at                 TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at                 TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_conversation_sessions_active
    ON conversation_sessions(story_id, place_id, status);

ALTER TABLE chat_logs ADD COLUMN conversation_session_id INTEGER;
ALTER TABLE chat_logs ADD COLUMN reply_to_log_id INTEGER;

INSERT OR IGNORE INTO schema_version (version, description)
VALUES (3, 'conversation sessions added');

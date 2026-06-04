CREATE TABLE IF NOT EXISTS ambient_states (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id          TEXT NOT NULL,
    scope_type        TEXT NOT NULL,
    scope_id          TEXT NOT NULL,
    factor_kind       TEXT NOT NULL,
    factor_key        TEXT NOT NULL,
    summary           TEXT NOT NULL,
    intensity         REAL NOT NULL DEFAULT 0.3,
    emotion_delta     JSON NOT NULL DEFAULT '{}',
    created_turn      INTEGER NOT NULL,
    expires_turn      INTEGER NOT NULL,
    last_applied_turn INTEGER,
    source            TEXT NOT NULL DEFAULT 'template',
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_ambient_states_active
    ON ambient_states(story_id, scope_type, scope_id, expires_turn);

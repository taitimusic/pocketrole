BEGIN;

CREATE TABLE IF NOT EXISTS conversation_motif_settings (
    story_id       TEXT NOT NULL,
    motif_id       TEXT NOT NULL,
    enabled        INTEGER NOT NULL DEFAULT 0,
    strength       TEXT NOT NULL DEFAULT 'moderate',
    cooldown_turns INTEGER NOT NULL DEFAULT 4,
    updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (story_id, motif_id),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS conversation_motif_runs (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id             TEXT NOT NULL,
    motif_id             TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'active',
    stage                TEXT NOT NULL DEFAULT 'seeded',
    seed_hook_id         INTEGER,
    seed_log_id          INTEGER,
    owner_char_id        TEXT,
    pickup_char_id       TEXT,
    place_id             TEXT,
    title                TEXT NOT NULL,
    description          TEXT NOT NULL,
    started_turn         INTEGER NOT NULL,
    last_advanced_turn   INTEGER NOT NULL,
    cooldown_until_turn  INTEGER,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE,
    FOREIGN KEY (seed_hook_id) REFERENCES story_hooks(id) ON DELETE SET NULL,
    FOREIGN KEY (seed_log_id) REFERENCES chat_logs(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_conversation_motif_runs_active
    ON conversation_motif_runs(story_id, status, motif_id, stage);

CREATE INDEX IF NOT EXISTS idx_conversation_motif_runs_seed_hook
    ON conversation_motif_runs(story_id, seed_hook_id);

INSERT OR IGNORE INTO schema_version (version, description)
VALUES (27, 'conversation motif settings and runs added');

COMMIT;

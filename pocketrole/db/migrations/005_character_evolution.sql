-- ============================================================
-- Migration 005: Character Evolution (v2 upgrade Phase B)
-- ============================================================

CREATE TABLE IF NOT EXISTS character_evolution (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    char_id          TEXT    NOT NULL,
    story_id         TEXT    NOT NULL,
    turn_number      INTEGER NOT NULL,
    field            TEXT    NOT NULL,
    previous_value   TEXT,
    new_value        TEXT    NOT NULL,
    reason           TEXT    NOT NULL,
    source_memory_id INTEGER,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (story_id)         REFERENCES stories(id)      ON DELETE CASCADE,
    FOREIGN KEY (source_memory_id) REFERENCES story_memory(id) ON DELETE SET NULL
);

-- (char_id, story_id) ごとに最大 turn_number の行が有効なオーバーレイとなる
CREATE INDEX IF NOT EXISTS idx_character_evolution_char
    ON character_evolution(char_id, story_id, turn_number DESC);

INSERT OR IGNORE INTO schema_version (version, description)
VALUES (5, 'character_evolution table added');

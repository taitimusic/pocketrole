BEGIN;

CREATE TABLE IF NOT EXISTS scene_scripts (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id             TEXT NOT NULL,
    turn_number          INTEGER NOT NULL,
    round_number         INTEGER,
    script_text          TEXT NOT NULL,
    director_persona_id  TEXT,
    chapter_id           TEXT,
    beat_phase           TEXT,
    generation_mode      TEXT,
    format_mode          TEXT,
    generation_metadata  TEXT,
    llm_provider         TEXT,
    llm_model            TEXT,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(story_id, turn_number),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_scene_scripts_story_turn
    ON scene_scripts(story_id, turn_number DESC);

INSERT OR IGNORE INTO schema_version (version, description)
VALUES (28, 'scene_scripts table for pre-turn director scene descriptions');

COMMIT;

CREATE TABLE IF NOT EXISTS story_dramatic_pressures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id TEXT NOT NULL,
    pressure_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    title TEXT,
    summary TEXT,
    focus_char_ids TEXT NOT NULL DEFAULT '[]',
    focus_place_id TEXT,
    dedupe_key TEXT NOT NULL,
    source_hook_id INTEGER,
    source_tension_id INTEGER,
    source_pattern_id INTEGER,
    source_episode_id INTEGER,
    source_relationship_mode_id INTEGER,
    source_canon_bit_id INTEGER,
    source_scene_id INTEGER,
    first_detected_turn INTEGER NOT NULL,
    last_detected_turn INTEGER NOT NULL,
    recurrence_count INTEGER NOT NULL DEFAULT 1,
    score REAL NOT NULL DEFAULT 0.5,
    urgency REAL NOT NULL DEFAULT 0.5,
    payoff_ready REAL NOT NULL DEFAULT 0.5,
    intent_alignment REAL NOT NULL DEFAULT 0.55,
    resolved_turn INTEGER,
    resolution_note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_story_dramatic_pressures_active_dedupe
ON story_dramatic_pressures (story_id, dedupe_key)
WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_story_dramatic_pressures_story_status_type
ON story_dramatic_pressures (story_id, status, pressure_type);

CREATE INDEX IF NOT EXISTS idx_story_dramatic_pressures_story_status_score
ON story_dramatic_pressures (story_id, status, score DESC);

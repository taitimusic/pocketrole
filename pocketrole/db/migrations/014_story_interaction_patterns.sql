BEGIN;

CREATE TABLE IF NOT EXISTS story_interaction_patterns (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id            TEXT NOT NULL,
    pattern_type        TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'active',
    title               TEXT NOT NULL,
    description         TEXT NOT NULL,
    involved_chars      JSON NOT NULL DEFAULT '[]',
    dedupe_key          TEXT NOT NULL,
    source_hook_id      INTEGER,
    source_tension_id   INTEGER,
    source_scene_id     INTEGER,
    source_event_id     INTEGER,
    first_detected_turn INTEGER NOT NULL,
    last_detected_turn  INTEGER NOT NULL,
    recurrence_count    INTEGER NOT NULL DEFAULT 1,
    intensity           REAL NOT NULL DEFAULT 0.5,
    confidence          REAL NOT NULL DEFAULT 0.5,
    resolved_turn       INTEGER,
    resolution_note     TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE,
    FOREIGN KEY (source_hook_id) REFERENCES story_hooks(id) ON DELETE SET NULL,
    FOREIGN KEY (source_tension_id) REFERENCES narrative_tensions(id) ON DELETE SET NULL,
    FOREIGN KEY (source_scene_id) REFERENCES story_scenes(id) ON DELETE SET NULL,
    FOREIGN KEY (source_event_id) REFERENCES relationship_events(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_story_interaction_patterns_active
    ON story_interaction_patterns(story_id, status, pattern_type);

CREATE INDEX IF NOT EXISTS idx_story_interaction_patterns_dedupe
    ON story_interaction_patterns(story_id, dedupe_key);

INSERT OR IGNORE INTO schema_version (version, description)
VALUES (14, 'story_interaction_patterns table added');

COMMIT;

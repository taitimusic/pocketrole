CREATE TABLE IF NOT EXISTS story_canon_bits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id TEXT NOT NULL,
    bit_type TEXT NOT NULL,
    motif_key TEXT NOT NULL,
    canon_level TEXT NOT NULL DEFAULT 'momentary_bit',
    status TEXT NOT NULL DEFAULT 'active',
    title TEXT,
    summary TEXT,
    focus_char_ids TEXT,
    focus_place_id TEXT,
    dedupe_key TEXT NOT NULL,
    evidence_sources TEXT,
    anchor_pattern_id INTEGER,
    anchor_episode_id INTEGER,
    anchor_relationship_mode_id INTEGER,
    anchor_scene_id INTEGER,
    first_detected_turn INTEGER NOT NULL,
    last_reinforced_turn INTEGER NOT NULL,
    recurrence_count INTEGER NOT NULL DEFAULT 1,
    confidence REAL NOT NULL DEFAULT 0.5,
    novelty REAL NOT NULL DEFAULT 0.5,
    intent_alignment REAL NOT NULL DEFAULT 0.55,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE,
    FOREIGN KEY (anchor_pattern_id) REFERENCES story_interaction_patterns(id) ON DELETE SET NULL,
    FOREIGN KEY (anchor_episode_id) REFERENCES story_episodes(id) ON DELETE SET NULL,
    FOREIGN KEY (anchor_relationship_mode_id) REFERENCES relationship_modes(id) ON DELETE SET NULL,
    FOREIGN KEY (anchor_scene_id) REFERENCES story_scenes(id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_story_canon_bits_active_dedupe
    ON story_canon_bits(story_id, dedupe_key)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_story_canon_bits_story_status_level
    ON story_canon_bits(story_id, status, canon_level);

CREATE INDEX IF NOT EXISTS idx_story_canon_bits_story_status_type
    ON story_canon_bits(story_id, status, bit_type);

INSERT OR IGNORE INTO schema_version(version, description)
VALUES (17, 'story_canon_bits table added');

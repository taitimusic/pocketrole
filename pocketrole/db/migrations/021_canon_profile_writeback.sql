-- ============================================================
-- Migration 021: Canon profile writeback overlays
-- ============================================================

ALTER TABLE character_evolution
    ADD COLUMN source_canon_bit_id INTEGER REFERENCES story_canon_bits(id) ON DELETE SET NULL;

ALTER TABLE story_canon_bits
    ADD COLUMN last_writeback_turn INTEGER;

ALTER TABLE story_canon_bits
    ADD COLUMN writeback_count INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS character_canon_overlays (
    story_id             TEXT    NOT NULL,
    char_id              TEXT    NOT NULL,
    overlay_json         JSON    NOT NULL DEFAULT '{}',
    version              INTEGER NOT NULL DEFAULT 1,
    last_written_turn    INTEGER,
    source_canon_bit_ids JSON    NOT NULL DEFAULT '[]',
    created_at           TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (story_id, char_id),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_character_canon_overlays_last_written
    ON character_canon_overlays(story_id, last_written_turn DESC);

INSERT OR IGNORE INTO schema_version (version, description)
VALUES (21, 'canon profile writeback overlays added');

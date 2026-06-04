BEGIN;

ALTER TABLE story_canon_bits ADD COLUMN last_reignited_turn INTEGER;
ALTER TABLE story_canon_bits ADD COLUMN reignition_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE story_hooks ADD COLUMN source_canon_bit_id INTEGER;

CREATE INDEX IF NOT EXISTS idx_story_hooks_open_canon_source
    ON story_hooks(story_id, status, source_canon_bit_id, priority DESC);

INSERT OR IGNORE INTO schema_version(version, description)
VALUES (20, 'canon reignition hook tracking added');

COMMIT;

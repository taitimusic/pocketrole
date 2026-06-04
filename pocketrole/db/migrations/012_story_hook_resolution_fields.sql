BEGIN;

ALTER TABLE story_hooks ADD COLUMN resolved_turn INTEGER;
ALTER TABLE story_hooks ADD COLUMN resolution_summary TEXT;

INSERT OR IGNORE INTO schema_version (version, description)
VALUES (12, 'story hook resolution fields added');

COMMIT;

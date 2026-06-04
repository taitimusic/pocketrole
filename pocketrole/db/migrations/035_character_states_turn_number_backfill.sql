BEGIN;

UPDATE character_states SET turn_number = 0 WHERE turn_number IS NULL;

INSERT OR IGNORE INTO schema_version (version, description)
VALUES (35, 'backfill NULL turn_number on character_states initial rows');

COMMIT;

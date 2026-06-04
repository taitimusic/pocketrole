BEGIN;

ALTER TABLE story_utterance_settings
    ADD COLUMN max_sentences INTEGER NOT NULL DEFAULT 3;

INSERT OR IGNORE INTO schema_version (version, description)
VALUES (32, 'story-level max sentence count for utterances');

COMMIT;

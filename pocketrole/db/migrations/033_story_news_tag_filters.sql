BEGIN;

CREATE TABLE IF NOT EXISTS story_news_tag_filters (
    story_id TEXT NOT NULL,
    tag_name TEXT NOT NULL COLLATE NOCASE,
    PRIMARY KEY (story_id, tag_name),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_story_news_tag_filters_story ON story_news_tag_filters(story_id);

INSERT OR IGNORE INTO schema_version (version, description)
VALUES (33, 'per-story news tag filter');

COMMIT;

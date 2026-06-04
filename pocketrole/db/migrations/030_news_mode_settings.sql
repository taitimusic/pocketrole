BEGIN;

-- per-story 時事モード設定（enabled/intensity）
CREATE TABLE IF NOT EXISTS story_news_mode_settings (
    story_id    TEXT NOT NULL PRIMARY KEY,
    enabled     INTEGER NOT NULL DEFAULT 0,
    intensity   TEXT NOT NULL DEFAULT 'low',     -- 'off' | 'low' | 'medium' | 'high'
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE
);

-- キャラが時事感想を述べた記事の履歴（同一記事の重複コメント防止）
CREATE TABLE IF NOT EXISTS news_commentary_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id    TEXT NOT NULL,
    char_id     TEXT NOT NULL,
    article_url TEXT NOT NULL,
    turn_number INTEGER NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (story_id, char_id, article_url),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_news_commentary_story_char
    ON news_commentary_history(story_id, char_id);

INSERT OR IGNORE INTO schema_version (version, description)
VALUES (30, 'news mode settings and commentary history added');

COMMIT;

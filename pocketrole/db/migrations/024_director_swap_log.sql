-- Migration 024: Director Swap Log
-- ユーザーによる監督交代の履歴を記録する。
-- from_persona_id が NULL の場合は初回設定（エンジン起動時の初期 active 設定）を意味する。

CREATE TABLE director_swap_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id        TEXT NOT NULL,
    turn_number     INTEGER NOT NULL,
    from_persona_id TEXT,              -- NULL = 初回設定
    to_persona_id   TEXT NOT NULL,
    reason          TEXT,              -- ユーザー記入の交代理由
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE
);
CREATE INDEX idx_swap_log_story
    ON director_swap_log(story_id, turn_number DESC);

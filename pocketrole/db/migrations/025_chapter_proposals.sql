-- ============================================================
-- Migration 025: Chapter Proposals (Phase 4 半自動 Chapter 生成)
-- ============================================================

-- chapter_proposals: LLM が生成した chapter 案（管理者承認待ち）
CREATE TABLE IF NOT EXISTS chapter_proposals (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id                TEXT    NOT NULL,
    theme                   TEXT    NOT NULL,
    proposed_at_turn        INTEGER NOT NULL DEFAULT 0,
    proposed_chapter_json   TEXT    NOT NULL,   -- JSON: 完全な chapter 定義
    conflict_seeds_json     TEXT,               -- JSON array of seed strings
    generated_by_persona_id TEXT,               -- 生成時の active persona id
    admin_status            TEXT    NOT NULL DEFAULT 'pending',  -- 'pending'|'approved'|'rejected'
    approved_turn           INTEGER,
    notes                   TEXT,
    created_at              TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chapter_proposals_story_status
    ON chapter_proposals (story_id, admin_status);

INSERT OR IGNORE INTO schema_version (version, description)
VALUES (25, 'chapter_proposals table added for Phase 4 semi-automatic chapter generation');

-- Migration 022: Chapter System
-- Chapter はストーリーの上位にある管理者定義のサブストーリー。
-- beat 構造（setup → complication → turning_point → resolution）を持ち、
-- world_injection で全キャラ prompt に設定を注入する。

CREATE TABLE story_chapters (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id        TEXT NOT NULL,
    chapter_id      TEXT NOT NULL,          -- YAML 定義上の ID
    title           TEXT NOT NULL,
    theme           TEXT,
    world_injection TEXT,                   -- 全キャラ prompt に注入するテキスト
    status          TEXT NOT NULL DEFAULT 'pending',
                                            -- 'pending' / 'active' / 'closed'
    start_condition TEXT,                   -- 'turn >= N' / 'sim_date >= X' / 'manual'
    current_beat    TEXT NOT NULL DEFAULT 'setup',
                                            -- 'setup' / 'complication' / 'turning_point' / 'resolution'
    opened_turn     INTEGER,
    closed_turn     INTEGER,
    close_reason    TEXT,
                                            -- 'completed' / 'aborted' / 'timed_out'
    carry_over_json TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX idx_chapters_story_chapter
    ON story_chapters(story_id, chapter_id);
CREATE INDEX idx_chapters_active
    ON story_chapters(story_id, status) WHERE status = 'active';

CREATE TABLE story_chapter_beats (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_db_id   INTEGER NOT NULL,       -- story_chapters.id
    phase           TEXT NOT NULL,
                                            -- 'setup' / 'complication' / 'turning_point' / 'resolution'
    description     TEXT,
    goal            TEXT,                   -- beat 到達の自然言語条件
    events_json     TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'pending',
                                            -- 'pending' / 'active' / 'reached' / 'skipped'
    reached_turn    INTEGER,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (chapter_db_id) REFERENCES story_chapters(id) ON DELETE CASCADE
);
CREATE INDEX idx_chapter_beats_chapter
    ON story_chapter_beats(chapter_db_id, phase);

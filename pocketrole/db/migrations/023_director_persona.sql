-- Migration 023: Director Persona
-- 架空の監督ペルソナが持つ美学パラメータと満足度評価のテーブル。
-- director_personas は 1 story に複数定義でき、is_active=1 が現在の active 監督。
-- director_satisfaction は N round ごとの満足度評価を蓄積する。

CREATE TABLE director_personas (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id        TEXT NOT NULL,
    persona_id      TEXT NOT NULL,           -- YAML 定義上の ID
    name            TEXT NOT NULL,
    aesthetic_json  TEXT NOT NULL DEFAULT '{}',   -- 美学パラメータ
    values_json     TEXT NOT NULL DEFAULT '[]',   -- 価値観リスト
    traits_json     TEXT NOT NULL DEFAULT '[]',   -- 演出上の癖タグ
    is_active       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX idx_personas_story_persona
    ON director_personas(story_id, persona_id);

CREATE TABLE director_satisfaction (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id            TEXT NOT NULL,
    persona_id          TEXT NOT NULL,
    turn_number         INTEGER NOT NULL,
    overall             REAL NOT NULL DEFAULT 0.0,    -- -1.0 ~ 1.0
    tension_sat         REAL NOT NULL DEFAULT 0.0,
    character_depth_sat REAL NOT NULL DEFAULT 0.0,
    pacing_sat          REAL NOT NULL DEFAULT 0.0,
    surprise_sat        REAL NOT NULL DEFAULT 0.0,
    dialogue_sat        REAL NOT NULL DEFAULT 0.0,
    atmosphere_sat      REAL NOT NULL DEFAULT 0.0,
    trend               TEXT NOT NULL DEFAULT 'flat',
                                        -- 'rising' / 'flat' / 'declining'
    details_json        TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE
);
CREATE INDEX idx_satisfaction_latest
    ON director_satisfaction(story_id, persona_id, turn_number DESC);

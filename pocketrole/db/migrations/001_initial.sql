-- ============================================================
-- Migration 001: Initial Schema v0.5
-- ============================================================

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    description TEXT
);

CREATE TABLE IF NOT EXISTS stories (
    id                TEXT    PRIMARY KEY,
    title             TEXT    NOT NULL,
    description       TEXT,
    world_rules       JSON,
    season_start      TEXT,
    turn_minutes      INTEGER NOT NULL DEFAULT 30,
    turn_interval_sec INTEGER NOT NULL DEFAULT 30,
    last_sim_time     TEXT,
    llm_provider      TEXT    NOT NULL DEFAULT 'ollama',
    llm_model         TEXT    NOT NULL DEFAULT '',
    is_active         INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS places (
    id              TEXT NOT NULL,
    story_id        TEXT NOT NULL,
    label           TEXT NOT NULL,
    zone            TEXT NOT NULL,
    atmosphere      TEXT,
    who_gathers     JSON,
    events_likely   JSON,
    access_note     TEXT,
    adjacent_places JSON,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (id, story_id),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS time_schedules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id        TEXT    NOT NULL,
    day_type        TEXT    NOT NULL,
    time_from       TEXT    NOT NULL,
    time_to         TEXT    NOT NULL,
    label           TEXT    NOT NULL,
    expected_places JSON,
    mood_modifier   TEXT,
    anomaly_note    TEXT,
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS event_calendar (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id      TEXT    NOT NULL,
    event_date    TEXT    NOT NULL,
    name          TEXT    NOT NULL,
    duration_days INTEGER NOT NULL DEFAULT 1,
    atmosphere    TEXT,
    emotion_impact JSON,
    force_place   TEXT,
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS anomaly_rules (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id          TEXT    NOT NULL,
    label             TEXT    NOT NULL,
    condition_json    JSON    NOT NULL,
    drama_potential   TEXT,
    suggested_reasons JSON,
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS characters (
    id                       TEXT    NOT NULL,
    story_id                 TEXT    NOT NULL,
    name_ja                  TEXT    NOT NULL,
    name_read                TEXT,
    gender                   TEXT,
    appearance               TEXT,
    personality_core         TEXT,
    speech                   JSON,
    strengths                JSON,
    weaknesses               JSON,
    current_goal             TEXT,
    current_worry            TEXT,
    secret                   TEXT,
    secret_reveal_condition  TEXT,
    emotion_default          JSON,
    favorite_places          JSON,
    move_tendency            JSON,
    expressions_available    JSON,
    image_path               TEXT,
    is_active                INTEGER NOT NULL DEFAULT 1,
    created_at               TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at               TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (id, story_id),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS character_states (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    char_id            TEXT    NOT NULL,
    story_id           TEXT    NOT NULL,
    sim_datetime       TEXT    NOT NULL,
    turn_number        INTEGER,
    current_place      TEXT    NOT NULL,
    previous_place     TEXT,
    move_reason        TEXT,
    current_action     TEXT,
    current_expression TEXT    NOT NULL DEFAULT 'neutral',
    stress             REAL    NOT NULL DEFAULT 0.3,
    motivation         REAL    NOT NULL DEFAULT 0.7,
    loneliness         REAL    NOT NULL DEFAULT 0.2,
    excitement         REAL    NOT NULL DEFAULT 0.5,
    recorded_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_char_states_latest
    ON character_states(char_id, story_id, recorded_at DESC);

CREATE TABLE IF NOT EXISTS relationships (
    story_id     TEXT NOT NULL,
    char_id_from TEXT NOT NULL,
    char_id_to   TEXT NOT NULL,
    trust        REAL NOT NULL DEFAULT 0.5,
    note         TEXT,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (story_id, char_id_from, char_id_to),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS chat_logs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id         TEXT    NOT NULL,
    sim_datetime     TEXT    NOT NULL,
    turn_number      INTEGER,
    char_id          TEXT    NOT NULL,
    msg_type         TEXT    NOT NULL,
    target_char_id   TEXT,
    place_id         TEXT,
    expression       TEXT    NOT NULL DEFAULT 'neutral',
    message          TEXT    NOT NULL,
    emotion_snapshot JSON,
    llm_provider     TEXT,
    llm_model        TEXT,
    posted_to_web    INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chat_logs_unposted
    ON chat_logs(story_id, posted_to_web);

CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    char_id     TEXT    NOT NULL,
    story_id    TEXT    NOT NULL,
    memory_type TEXT    NOT NULL DEFAULT 'short',
    content     TEXT    NOT NULL,
    importance  REAL    NOT NULL DEFAULT 0.5,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_memories_char
    ON memories(char_id, story_id, created_at);

-- マイグレーション記録
INSERT OR IGNORE INTO schema_version (version, description)
VALUES (1, 'initial schema v0.5');

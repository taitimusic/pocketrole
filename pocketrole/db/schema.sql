-- ============================================================
-- PocketRole DB Schema v0.5
-- NOTE: PRAGMAはここに書かない。db_manager.py が接続のたびに設定する。
-- ============================================================

-- ============================================================
-- schema_version: マイグレーション管理
-- ============================================================
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    description TEXT
);

-- ============================================================
-- stories: ストーリー世界の定義
-- ターン速度・LLMプロバイダー・再開情報もここで管理
-- ============================================================
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

-- ============================================================
-- places: 場所マップ
-- ============================================================
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
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (id, story_id),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE
);

-- ============================================================
-- time_schedules: 時間割スケジュール
-- ============================================================
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

-- ============================================================
-- event_calendar: イベント暦
-- ============================================================
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

-- ============================================================
-- anomaly_rules: 異常検知ルール
-- ============================================================
CREATE TABLE IF NOT EXISTS anomaly_rules (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id          TEXT    NOT NULL,
    label             TEXT    NOT NULL,
    condition_json    JSON    NOT NULL,
    drama_potential   TEXT,
    suggested_reasons JSON,
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE
);

-- ============================================================
-- characters: キャラクター基本設定
-- ============================================================
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

-- ============================================================
-- character_states: キャラクター現在状態・移動履歴
-- ============================================================
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

-- ============================================================
-- relationships: キャラ間信頼度（動的に変化）
-- ============================================================
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

-- ============================================================
-- chat_logs: 全発言ログ
-- ============================================================
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
    conversation_session_id INTEGER,
    reply_to_log_id  INTEGER,
    emotion_snapshot JSON,
    llm_provider     TEXT,
    llm_model        TEXT,
    posted_to_web    INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chat_logs_unposted
    ON chat_logs(story_id, posted_to_web);

-- ============================================================
-- conversation_sessions: 継続会話の状態
-- ============================================================
CREATE TABLE IF NOT EXISTS conversation_sessions (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id                 TEXT    NOT NULL,
    place_id                 TEXT    NOT NULL,
    participant_ids          JSON    NOT NULL,
    status                   TEXT    NOT NULL DEFAULT 'active',
    last_speaker_id          TEXT,
    last_log_id              INTEGER,
    started_at_sim_datetime  TEXT    NOT NULL,
    last_activity_sim_datetime TEXT  NOT NULL,
    last_turn_number         INTEGER,
    created_at               TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at               TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_conversation_sessions_active
    ON conversation_sessions(story_id, place_id, status);

-- ============================================================
-- memories: キャラ短期記憶
-- ============================================================
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

-- ============================================================
-- conversation_motif_settings / conversation_motif_runs:
-- 会話発生パターン（会話輪舞モード）の設定と進行状態
-- ============================================================
CREATE TABLE IF NOT EXISTS conversation_motif_settings (
    story_id       TEXT NOT NULL,
    motif_id       TEXT NOT NULL,
    enabled        INTEGER NOT NULL DEFAULT 0,
    strength       TEXT NOT NULL DEFAULT 'moderate',
    cooldown_turns INTEGER NOT NULL DEFAULT 4,
    updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (story_id, motif_id),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS conversation_motif_runs (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id             TEXT NOT NULL,
    motif_id             TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'active',
    stage                TEXT NOT NULL DEFAULT 'seeded',
    seed_hook_id         INTEGER,
    seed_log_id          INTEGER,
    owner_char_id        TEXT,
    pickup_char_id       TEXT,
    place_id             TEXT,
    title                TEXT NOT NULL,
    description          TEXT NOT NULL,
    started_turn         INTEGER NOT NULL,
    last_advanced_turn   INTEGER NOT NULL,
    cooldown_until_turn  INTEGER,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE,
    FOREIGN KEY (seed_hook_id) REFERENCES story_hooks(id) ON DELETE SET NULL,
    FOREIGN KEY (seed_log_id) REFERENCES chat_logs(id) ON DELETE SET NULL
);

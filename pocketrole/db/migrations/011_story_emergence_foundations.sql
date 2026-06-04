BEGIN;

CREATE TABLE IF NOT EXISTS story_scenes (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id          TEXT NOT NULL,
    scene_type        TEXT NOT NULL DEFAULT 'conversation',
    status            TEXT NOT NULL DEFAULT 'active',
    place_id          TEXT,
    focus_char_ids    JSON NOT NULL DEFAULT '[]',
    objective         TEXT,
    dominant_tension_id INTEGER,
    opened_turn       INTEGER,
    closed_turn       INTEGER,
    outcome_type      TEXT,
    outcome_summary   TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS scene_participants (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    scene_id          INTEGER NOT NULL,
    char_id           TEXT NOT NULL,
    role              TEXT NOT NULL DEFAULT 'observer',
    join_turn         INTEGER,
    leave_turn        INTEGER,
    speak_budget      INTEGER NOT NULL DEFAULT 0,
    times_spoken      INTEGER NOT NULL DEFAULT 0,
    last_spoken_turn  INTEGER,
    state             TEXT NOT NULL DEFAULT 'active',
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (scene_id) REFERENCES story_scenes(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS story_hooks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id          TEXT NOT NULL,
    hook_type         TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'open',
    owner_char_id     TEXT,
    target_char_id    TEXT,
    title             TEXT NOT NULL,
    description       TEXT NOT NULL,
    priority          REAL NOT NULL DEFAULT 0.5,
    due_turn          INTEGER,
    source_scene_id   INTEGER,
    source_log_id     INTEGER,
    resolution_log_id INTEGER,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE,
    FOREIGN KEY (source_scene_id) REFERENCES story_scenes(id) ON DELETE SET NULL,
    FOREIGN KEY (source_log_id) REFERENCES chat_logs(id) ON DELETE SET NULL,
    FOREIGN KEY (resolution_log_id) REFERENCES chat_logs(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_story_hooks_open
    ON story_hooks(story_id, status, priority DESC);

CREATE TABLE IF NOT EXISTS character_drives (
    story_id          TEXT NOT NULL,
    char_id           TEXT NOT NULL,
    current_goal      TEXT,
    current_need      TEXT,
    avoidance         TEXT,
    taboo_topics      JSON NOT NULL DEFAULT '[]',
    urgency           REAL NOT NULL DEFAULT 0.5,
    stability         REAL NOT NULL DEFAULT 0.5,
    updated_turn      INTEGER,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (story_id, char_id),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS relationship_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id          TEXT NOT NULL,
    char_id_from      TEXT NOT NULL,
    char_id_to        TEXT NOT NULL,
    event_type        TEXT NOT NULL,
    delta_trust       REAL NOT NULL DEFAULT 0.0,
    delta_affinity    REAL NOT NULL DEFAULT 0.0,
    delta_tension     REAL NOT NULL DEFAULT 0.0,
    summary           TEXT,
    source_log_id     INTEGER,
    scene_id          INTEGER,
    turn_number       INTEGER,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE,
    FOREIGN KEY (source_log_id) REFERENCES chat_logs(id) ON DELETE SET NULL,
    FOREIGN KEY (scene_id) REFERENCES story_scenes(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS generation_quality_issues (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id          TEXT NOT NULL,
    log_id            INTEGER,
    scene_id          INTEGER,
    issue_type        TEXT NOT NULL,
    severity          TEXT NOT NULL DEFAULT 'warning',
    details           JSON NOT NULL DEFAULT '{}',
    auto_action       TEXT NOT NULL DEFAULT 'accept',
    created_turn      INTEGER,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE,
    FOREIGN KEY (log_id) REFERENCES chat_logs(id) ON DELETE SET NULL,
    FOREIGN KEY (scene_id) REFERENCES story_scenes(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS character_profile_overlays (
    story_id             TEXT NOT NULL,
    char_id              TEXT NOT NULL,
    overlay_json         JSON NOT NULL DEFAULT '{}',
    version              INTEGER NOT NULL DEFAULT 1,
    last_committed_turn  INTEGER,
    source_evolution_id  INTEGER,
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (story_id, char_id),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE,
    FOREIGN KEY (source_evolution_id) REFERENCES character_evolution(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS character_growth_candidates (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id               TEXT NOT NULL,
    char_id                TEXT NOT NULL,
    field                  TEXT NOT NULL,
    candidate_value        TEXT NOT NULL,
    reason                 TEXT NOT NULL,
    experience_score       REAL NOT NULL DEFAULT 0.5,
    identity_impact_score  REAL NOT NULL DEFAULT 0.5,
    confidence             REAL NOT NULL DEFAULT 0.5,
    source_memory_id       INTEGER,
    source_hook_id         INTEGER,
    source_log_id          INTEGER,
    source_scene_id        INTEGER,
    detected_turn          INTEGER NOT NULL,
    status                 TEXT NOT NULL DEFAULT 'pending',
    created_at             TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE,
    FOREIGN KEY (source_memory_id) REFERENCES story_memory(id) ON DELETE SET NULL,
    FOREIGN KEY (source_hook_id) REFERENCES story_hooks(id) ON DELETE SET NULL,
    FOREIGN KEY (source_log_id) REFERENCES chat_logs(id) ON DELETE SET NULL,
    FOREIGN KEY (source_scene_id) REFERENCES story_scenes(id) ON DELETE SET NULL
);

ALTER TABLE chat_logs ADD COLUMN scene_id INTEGER;
ALTER TABLE chat_logs ADD COLUMN speaker_intent TEXT;
ALTER TABLE chat_logs ADD COLUMN quality_score REAL;
ALTER TABLE chat_logs ADD COLUMN quality_flags JSON;
ALTER TABLE chat_logs ADD COLUMN state_effect_summary TEXT;
ALTER TABLE chat_logs ADD COLUMN source_intervention_id INTEGER;
ALTER TABLE chat_logs ADD COLUMN generation_attempt INTEGER NOT NULL DEFAULT 1;

ALTER TABLE relationships ADD COLUMN affinity REAL NOT NULL DEFAULT 0.5;
ALTER TABLE relationships ADD COLUMN tension REAL NOT NULL DEFAULT 0.0;
ALTER TABLE relationships ADD COLUMN familiarity REAL NOT NULL DEFAULT 0.5;
ALTER TABLE relationships ADD COLUMN last_event_turn INTEGER;
ALTER TABLE relationships ADD COLUMN last_event_summary TEXT;

ALTER TABLE conversation_sessions ADD COLUMN scene_id INTEGER;
ALTER TABLE conversation_sessions ADD COLUMN session_kind TEXT NOT NULL DEFAULT 'conversation';
ALTER TABLE conversation_sessions ADD COLUMN closure_reason TEXT;

INSERT OR IGNORE INTO schema_version (version, description)
VALUES (11, 'story emergence foundations added');

COMMIT;

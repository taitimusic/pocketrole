-- ============================================================
-- Migration 009: Hosted admin/auth/publication metadata
-- ============================================================

CREATE TABLE IF NOT EXISTS admin_users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT    NOT NULL UNIQUE,
    password_hash   TEXT    NOT NULL,
    password_salt   TEXT    NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_user_id   INTEGER NOT NULL,
    session_token   TEXT    NOT NULL UNIQUE,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    last_used_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    expires_at      TEXT    NOT NULL,
    FOREIGN KEY (admin_user_id) REFERENCES admin_users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS admin_api_tokens (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_user_id   INTEGER,
    label           TEXT    NOT NULL,
    token_hash      TEXT    NOT NULL UNIQUE,
    token_prefix    TEXT    NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    last_used_at    TEXT,
    revoked_at      TEXT,
    FOREIGN KEY (admin_user_id) REFERENCES admin_users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS story_publications (
    story_id             TEXT PRIMARY KEY,
    visibility           TEXT    NOT NULL DEFAULT 'draft',
    page_size_scenes     INTEGER NOT NULL DEFAULT 10,
    published_at         TEXT,
    last_build_at        TEXT,
    last_success_at      TEXT,
    last_error           TEXT,
    latest_published_turn INTEGER,
    updated_at           TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS system_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS admin_audit_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_type      TEXT    NOT NULL,
    actor_label     TEXT    NOT NULL,
    action          TEXT    NOT NULL,
    story_id        TEXT,
    result          TEXT    NOT NULL,
    payload_summary TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE SET NULL
);

INSERT OR IGNORE INTO system_settings (key, value)
VALUES ('archive_publish_interval_minutes', '5');

INSERT OR IGNORE INTO schema_version (version, description)
VALUES (9, 'admin auth and publication metadata added');

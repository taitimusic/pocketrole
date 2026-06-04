-- ============================================================
-- Migration 006: Narrative Tensions (v2 upgrade Phase B)
-- ============================================================

CREATE TABLE IF NOT EXISTS narrative_tensions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id        TEXT    NOT NULL,
    tension_type    TEXT    NOT NULL,
    description     TEXT    NOT NULL,
    involved_chars  JSON,
    intensity       REAL    NOT NULL DEFAULT 0.3,
    detected_turn   INTEGER NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'simmering',
    resolved_turn   INTEGER,
    resolution_note TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_narrative_tensions_active
    ON narrative_tensions(story_id, status);

INSERT OR IGNORE INTO schema_version (version, description)
VALUES (6, 'narrative_tensions table added');

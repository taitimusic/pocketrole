-- ============================================================
-- Migration 007: Director Interventions (v2 upgrade Phase B)
-- ============================================================

CREATE TABLE IF NOT EXISTS director_interventions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id            TEXT    NOT NULL,
    intervention_type   TEXT    NOT NULL,
    title               TEXT    NOT NULL,
    description         TEXT    NOT NULL,
    prompt_injection    TEXT    NOT NULL,
    scope               TEXT    NOT NULL DEFAULT 'all',
    tension_id          INTEGER,
    active_from_turn    INTEGER NOT NULL,
    active_until_turn   INTEGER,
    status              TEXT    NOT NULL DEFAULT 'active',
    resolution_summary  TEXT,
    created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (story_id)   REFERENCES stories(id)            ON DELETE CASCADE,
    FOREIGN KEY (tension_id) REFERENCES narrative_tensions(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_director_interventions_active
    ON director_interventions(story_id, status);

INSERT OR IGNORE INTO schema_version (version, description)
VALUES (7, 'director_interventions table added');

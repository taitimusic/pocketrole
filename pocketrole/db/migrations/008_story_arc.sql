-- ============================================================
-- Migration 008: Story Arc + Novel Output (v2 upgrade Phase C)
-- ============================================================

CREATE TABLE IF NOT EXISTS story_arc (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id        TEXT    NOT NULL,
    arc_type        TEXT    NOT NULL,
    title           TEXT    NOT NULL,
    summary         TEXT    NOT NULL,
    turn_from       INTEGER NOT NULL,
    turn_to         INTEGER,
    theme           TEXT,
    tension_level   REAL    NOT NULL DEFAULT 0.5,
    parent_arc_id   INTEGER,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (story_id)      REFERENCES stories(id)   ON DELETE CASCADE,
    FOREIGN KEY (parent_arc_id) REFERENCES story_arc(id)
);

CREATE INDEX IF NOT EXISTS idx_story_arc_story_type
    ON story_arc(story_id, arc_type);

CREATE TABLE IF NOT EXISTS novel_output (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id        TEXT    NOT NULL,
    arc_id          INTEGER NOT NULL,
    content_type    TEXT    NOT NULL,
    content         TEXT    NOT NULL,
    source_log_ids  TEXT,
    ordering        INTEGER NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (story_id) REFERENCES stories(id)   ON DELETE CASCADE,
    FOREIGN KEY (arc_id)   REFERENCES story_arc(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_novel_output_arc
    ON novel_output(arc_id, ordering);

INSERT OR IGNORE INTO schema_version (version, description)
VALUES (8, 'story_arc and novel_output tables added');

-- ============================================================
-- Migration 013: story_arc.source_scene_id for scene-close prose
-- ============================================================

ALTER TABLE story_arc
    ADD COLUMN source_scene_id INTEGER;

CREATE INDEX IF NOT EXISTS idx_story_arc_story_source_scene
    ON story_arc(story_id, source_scene_id);

INSERT OR IGNORE INTO schema_version (version, description)
VALUES (13, 'story_arc source_scene_id added');

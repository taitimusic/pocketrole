DROP INDEX IF EXISTS idx_relationship_modes_active_pair;

CREATE UNIQUE INDEX IF NOT EXISTS idx_relationship_modes_active_pair_mode
    ON relationship_modes(story_id, char_id_from, char_id_to, mode_type)
    WHERE status = 'active';

"""Tests for engine/growth_engine.py."""

from __future__ import annotations

from unittest.mock import AsyncMock

from engine.config import GrowthEngineConfig
from engine.growth_engine import (
    SUPPORTED_GROWTH_FIELDS,
    GrowthEngine,
    _candidate_growth_payload_fragments,
    _is_generic_cue_dominated,
    _parse_growth_candidates,
    _validate_growth_candidate,
)
from tests._async_harness import async_to_sync


def _make_cfg(**overrides) -> GrowthEngineConfig:
    cfg = GrowthEngineConfig(enabled=True, rolling_window_turns=20, immediate_commit_identity_threshold=0.85)
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def _make_char(**overrides) -> dict:
    char = {
        "id": "char_a",
        "name_ja": "アリス",
        "current_goal": "音楽を極める",
        "current_worry": "自信がない",
        "personality_core": "慎重で理性的",
    }
    char.update(overrides)
    return char


def test_parse_growth_candidates_sanitizes_invalid_ids() -> None:
    text = """
    [
      {
        "field": "current_goal",
        "candidate_value": "逃げない",
        "reason": "大きな約束を果たした",
        "experience_score": 0.9,
        "identity_impact_score": 0.86,
        "confidence": 0.8,
        "source_memory_id": 999,
        "source_hook_id": 4,
        "source_log_id": 77,
        "source_scene_id": 12
      }
    ]
    """

    parsed = _parse_growth_candidates(
        text,
        valid_memory_ids={1, 2},
        valid_hook_ids={4},
        valid_log_ids={10},
        valid_scene_ids={12},
    )

    assert len(parsed) == 1
    assert parsed[0]["source_memory_id"] is None
    assert parsed[0]["source_hook_id"] == 4
    assert parsed[0]["source_log_id"] is None
    assert parsed[0]["source_scene_id"] == 12


def test_parse_growth_candidates_normalizes_qualitative_scores() -> None:
    text = """
    [
      {
        "field": "current_goal",
        "candidate_value": "前に出る",
        "reason": "周囲に背中を押された",
        "experience_score": "high",
        "identity_impact_score": "medium",
        "confidence": "高い"
      }
    ]
    """

    parsed = _parse_growth_candidates(
        text,
        valid_memory_ids=set(),
        valid_hook_ids=set(),
        valid_log_ids=set(),
        valid_scene_ids=set(),
    )

    assert len(parsed) == 1
    assert parsed[0]["experience_score"] == 0.75
    assert parsed[0]["identity_impact_score"] == 0.5
    assert parsed[0]["confidence"] == 0.75


def test_parse_growth_candidates_accepts_single_object_payload() -> None:
    text = """
    {
      "field": "current_goal",
      "candidate_value": "踏み出す",
      "reason": "背中を押された",
      "experience_score": 0.8,
      "identity_impact_score": 0.65,
      "confidence": 0.75
    }
    """

    parsed = _parse_growth_candidates(
        text,
        valid_memory_ids=set(),
        valid_hook_ids=set(),
        valid_log_ids=set(),
        valid_scene_ids=set(),
    )

    assert len(parsed) == 1
    assert parsed[0]["field"] == "current_goal"
    assert parsed[0]["candidate_value"] == "踏み出す"


def test_parse_growth_candidates_repairs_truncated_single_object_payload() -> None:
    text = """
    {"field":"current_goal","candidate_value":"踏み出す","reason":"背中を押された",
     "experience_score":0.8,"identity_impact_score":0.65,"confidence":0.75
    """

    parsed = _parse_growth_candidates(
        text,
        valid_memory_ids=set(),
        valid_hook_ids=set(),
        valid_log_ids=set(),
        valid_scene_ids=set(),
    )

    assert len(parsed) == 1
    assert parsed[0]["field"] == "current_goal"
    assert parsed[0]["candidate_value"] == "踏み出す"


def test_parse_growth_candidates_accepts_explanatory_text_and_trailing_comma_array() -> None:
    text = """
    候補はこれです。
    [
      {
        "field": "current_goal",
        "candidate_value": "踏み出す",
        "reason": "背中を押された",
        "experience_score": 0.8,
        "identity_impact_score": 0.65,
        "confidence": 0.75
      },
    ]
    """

    parsed = _parse_growth_candidates(
        text,
        valid_memory_ids=set(),
        valid_hook_ids=set(),
        valid_log_ids=set(),
        valid_scene_ids=set(),
    )

    assert len(parsed) == 1
    assert parsed[0]["field"] == "current_goal"
    assert parsed[0]["candidate_value"] == "踏み出す"


def test_parse_growth_candidates_repairs_truncated_array_payload() -> None:
    text = """
    [
      {
        "field": "current_goal",
        "candidate_value": "踏み出す",
        "reason": "背中を押された",
        "experience_score": 0.8,
        "identity_impact_score": 0.65,
        "confidence": 0.75
      }
    """

    parsed = _parse_growth_candidates(
        text,
        valid_memory_ids=set(),
        valid_hook_ids=set(),
        valid_log_ids=set(),
        valid_scene_ids=set(),
    )

    assert len(parsed) == 1
    assert parsed[0]["field"] == "current_goal"
    assert parsed[0]["candidate_value"] == "踏み出す"


def test_parse_growth_candidates_repairs_trailing_comma_inside_object() -> None:
    text = """
    {
      "field": "current_goal",
      "candidate_value": "踏み出す",
      "reason": "背中を押された",
      "experience_score": 0.8,
      "identity_impact_score": 0.65,
      "confidence": 0.75,
    }
    """

    parsed = _parse_growth_candidates(
        text,
        valid_memory_ids=set(),
        valid_hook_ids=set(),
        valid_log_ids=set(),
        valid_scene_ids=set(),
    )

    assert len(parsed) == 1
    assert parsed[0]["field"] == "current_goal"
    assert parsed[0]["candidate_value"] == "踏み出す"


def test_parse_growth_candidates_accepts_bullet_preface_and_trailing_note() -> None:
    text = """
    候補メモ:
    - まず current_goal を見る

    [
      {
        "field": "current_goal",
        "candidate_value": "踏み出す",
        "reason": "背中を押された",
        "experience_score": 0.8,
        "identity_impact_score": 0.65,
        "confidence": 0.75
      }
    ]

    注: 他の候補は保留。
    """

    parsed = _parse_growth_candidates(
        text,
        valid_memory_ids=set(),
        valid_hook_ids=set(),
        valid_log_ids=set(),
        valid_scene_ids=set(),
    )

    assert len(parsed) == 1
    assert parsed[0]["field"] == "current_goal"
    assert parsed[0]["candidate_value"] == "踏み出す"


def test_parse_growth_candidates_accepts_first_valid_object_from_multiple_objects() -> None:
    text = """
    {"field":"current_goal","candidate_value":"踏み出す","reason":"背中を押された",
     "experience_score":0.8,"identity_impact_score":0.65,"confidence":0.75}
    {"field":"current_worry","candidate_value":"疑いすぎる","reason":"まだ揺れている",
     "experience_score":0.6,"identity_impact_score":0.55,"confidence":0.5}
    """

    parsed = _parse_growth_candidates(
        text,
        valid_memory_ids=set(),
        valid_hook_ids=set(),
        valid_log_ids=set(),
        valid_scene_ids=set(),
    )

    assert len(parsed) == 1
    assert parsed[0]["field"] == "current_goal"
    assert parsed[0]["candidate_value"] == "踏み出す"


def test_parse_growth_candidates_returns_empty_list_for_explicit_empty_array() -> None:
    parsed = _parse_growth_candidates(
        "前置き\n[]\n注記",
        valid_memory_ids=set(),
        valid_hook_ids=set(),
        valid_log_ids=set(),
        valid_scene_ids=set(),
    )

    assert parsed == []


@async_to_sync
async def test_growth_engine_skips_without_evidence() -> None:
    db = AsyncMock()
    db.get_relevant_story_memories = AsyncMock(return_value=[])
    db.get_relevant_story_hooks = AsyncMock(return_value=[])
    db.get_recent_relationship_events_for_character = AsyncMock(return_value=[])
    db.get_character_profile_overlay = AsyncMock(return_value=None)
    db.get_latest_evolution_overlay = AsyncMock(return_value={})
    router = AsyncMock()

    engine = GrowthEngine("test_story", db, router, _make_cfg(), llm_provider="ollama", llm_model="test")

    await engine.process_round(5, [_make_char()])

    router.generate.assert_not_awaited()
    db.insert_character_growth_candidate.assert_not_awaited()


@async_to_sync
async def test_growth_engine_immediate_commit_updates_overlay_and_evolution() -> None:
    db = AsyncMock()
    db.get_relevant_story_memories = AsyncMock(
        return_value=[{"id": 1, "summary": "約束を果たした。", "importance": 0.9}]
    )
    db.get_relevant_story_hooks = AsyncMock(return_value=[])
    db.get_recent_relationship_events_for_character = AsyncMock(return_value=[])
    db.get_character_profile_overlay = AsyncMock(
        return_value={"overlay_json": {"current_goal": "音楽を極める"}, "version": 2}
    )
    db.get_latest_evolution_overlay = AsyncMock(return_value={})
    db.insert_character_growth_candidate = AsyncMock(return_value=11)
    db.get_pending_growth_candidates = AsyncMock(return_value=[])
    db.insert_evolution = AsyncMock(return_value=21)
    db.upsert_character_profile_overlay = AsyncMock()
    db.update_character_growth_candidate_status = AsyncMock()
    router = AsyncMock()
    router.generate = AsyncMock(
        return_value=type(
            "Resp",
            (),
            {
                "text": """
                [
                  {
                    "field": "current_goal",
                    "candidate_value": "もう逃げない",
                    "reason": "重要な約束を果たした",
                    "experience_score": 0.92,
                    "identity_impact_score": 0.9,
                    "confidence": 0.82,
                    "source_memory_id": 1
                  }
                ]
                """,
            },
        )()
    )

    engine = GrowthEngine("test_story", db, router, _make_cfg(), llm_provider="ollama", llm_model="test")

    await engine.process_round(8, [_make_char()])

    db.insert_evolution.assert_awaited_once()
    db.upsert_character_profile_overlay.assert_awaited_once()
    overlay_payload = db.upsert_character_profile_overlay.await_args.args[2]
    assert overlay_payload["overlay_json"]["current_goal"] == "もう逃げない"
    db.update_character_growth_candidate_status.assert_awaited_once_with(11, "committed")


@async_to_sync
async def test_growth_engine_commits_after_accumulated_threshold() -> None:
    db = AsyncMock()
    db.get_relevant_story_memories = AsyncMock(
        return_value=[{"id": 1, "summary": "背中を押された。", "importance": 0.7}]
    )
    db.get_relevant_story_hooks = AsyncMock(return_value=[])
    db.get_recent_relationship_events_for_character = AsyncMock(return_value=[])
    db.get_character_profile_overlay = AsyncMock(return_value=None)
    db.get_latest_evolution_overlay = AsyncMock(return_value={"current_goal": "様子を見る"})
    db.insert_character_growth_candidate = AsyncMock(return_value=16)
    db.get_pending_growth_candidates = AsyncMock(
        return_value=[
            {
                "id": 4,
                "field": "current_goal",
                "candidate_value": "踏み出す",
                "experience_score": 0.7,
                "confidence": 0.7,
                "detected_turn": 8,
                "status": "pending",
            },
            {
                "id": 9,
                "field": "current_goal",
                "candidate_value": "踏み出す",
                "experience_score": 0.9,
                "confidence": 0.8,
                "detected_turn": 10,
                "status": "pending",
            },
        ]
    )
    db.insert_evolution = AsyncMock(return_value=33)
    db.upsert_character_profile_overlay = AsyncMock()
    db.update_character_growth_candidate_status = AsyncMock()
    router = AsyncMock()
    router.generate = AsyncMock(
        return_value=type(
            "Resp",
            (),
            {
                "text": """
                [
                  {
                    "field": "current_goal",
                    "candidate_value": "踏み出す",
                    "reason": "経験が積み重なった",
                    "experience_score": 0.9,
                    "identity_impact_score": 0.6,
                    "confidence": 0.8,
                    "source_memory_id": 1
                  }
                ]
                """,
            },
        )()
    )

    engine = GrowthEngine("test_story", db, router, _make_cfg(), llm_provider="ollama", llm_model="test")

    await engine.process_round(10, [_make_char()])

    db.insert_evolution.assert_awaited_once()
    committed_ids = [call.args[0] for call in db.update_character_growth_candidate_status.await_args_list]
    assert committed_ids == [16, 4, 9]


@async_to_sync
async def test_growth_engine_repairs_length_truncated_candidate_json() -> None:
    db = AsyncMock()
    db.get_relevant_story_memories = AsyncMock(
        return_value=[{"id": 1, "summary": "約束を果たした。", "importance": 0.9}]
    )
    db.get_relevant_story_hooks = AsyncMock(return_value=[])
    db.get_recent_relationship_events_for_character = AsyncMock(return_value=[])
    db.get_character_profile_overlay = AsyncMock(return_value=None)
    db.get_latest_evolution_overlay = AsyncMock(return_value={})
    db.insert_character_growth_candidate = AsyncMock(return_value=11)
    db.get_pending_growth_candidates = AsyncMock(return_value=[])
    db.insert_evolution = AsyncMock(return_value=21)
    db.upsert_character_profile_overlay = AsyncMock()
    db.update_character_growth_candidate_status = AsyncMock()
    router = AsyncMock()
    router.generate = AsyncMock(
        side_effect=[
            type(
                "Resp",
                (),
                {
                    "text": '[{"field":"current_goal","candidate_value":"もう逃げない"',
                    "done_reason": "length",
                },
            )(),
            type(
                "Resp",
                (),
                {
                    "text": """
                    [
                      {
                        "field": "current_goal",
                        "candidate_value": "もう逃げない",
                        "reason": "重要な約束を果たした",
                        "experience_score": 0.92,
                        "identity_impact_score": 0.9,
                        "confidence": 0.82,
                        "source_memory_id": 1
                      }
                    ]
                    """,
                    "done_reason": "stop",
                },
            )(),
        ]
    )

    engine = GrowthEngine("test_story", db, router, _make_cfg(), llm_provider="ollama", llm_model="test")

    await engine.process_round(8, [_make_char()])

    db.insert_evolution.assert_awaited_once()
    assert router.generate.await_count == 2


@async_to_sync
async def test_growth_engine_accepts_qualitative_score_labels_without_crashing() -> None:
    db = AsyncMock()
    db.get_relevant_story_memories = AsyncMock(
        return_value=[{"id": 1, "summary": "約束を果たした。", "importance": 0.9}]
    )
    db.get_relevant_story_hooks = AsyncMock(return_value=[])
    db.get_recent_relationship_events_for_character = AsyncMock(return_value=[])
    db.get_active_tensions = AsyncMock(return_value=[])
    db.get_recent_closed_story_scenes = AsyncMock(return_value=[])
    db.get_character_profile_overlay = AsyncMock(return_value=None)
    db.get_latest_evolution_overlay = AsyncMock(return_value={})
    db.insert_character_growth_candidate = AsyncMock(return_value=11)
    db.get_pending_growth_candidates = AsyncMock(return_value=[])
    db.insert_evolution = AsyncMock(return_value=21)
    db.upsert_character_profile_overlay = AsyncMock()
    db.update_character_growth_candidate_status = AsyncMock()
    router = AsyncMock()
    router.generate = AsyncMock(
        return_value=type(
            "Resp",
            (),
            {
                "text": """
                [
                  {
                    "field": "current_goal",
                    "candidate_value": "もう逃げない",
                    "reason": "重要な約束を果たした",
                    "experience_score": "high",
                    "identity_impact_score": "very_high",
                    "confidence": "high",
                    "source_memory_id": 1
                  }
                ]
                """,
                "done_reason": "stop",
            },
        )()
    )

    engine = GrowthEngine("test_story", db, router, _make_cfg(), llm_provider="ollama", llm_model="test")

    await engine.process_round(8, [_make_char()])

    db.insert_character_growth_candidate.assert_awaited_once()
    db.insert_evolution.assert_awaited_once()
    result = engine.get_last_round_results()["char_a"]
    assert result["result"] == "committed"


@async_to_sync
async def test_growth_engine_parse_failed_writes_nothing() -> None:
    db = AsyncMock()
    db.get_relevant_story_memories = AsyncMock(
        return_value=[{"id": 1, "summary": "約束を果たした。", "importance": 0.9}]
    )
    db.get_relevant_story_hooks = AsyncMock(return_value=[])
    db.get_recent_relationship_events_for_character = AsyncMock(return_value=[])
    db.get_active_tensions = AsyncMock(return_value=[])
    db.get_recent_closed_story_scenes = AsyncMock(return_value=[])
    db.get_character_profile_overlay = AsyncMock(return_value=None)
    db.get_latest_evolution_overlay = AsyncMock(return_value={})
    db.insert_character_growth_candidate = AsyncMock()
    db.insert_evolution = AsyncMock()
    db.upsert_character_profile_overlay = AsyncMock()
    db.insert_generation_quality_issue = AsyncMock()
    router = AsyncMock()
    router.generate = AsyncMock(
        side_effect=[
            type("Resp", (), {"text": '[{"field":"current_goal"', "done_reason": "length"})(),
            type("Resp", (), {"text": "{broken", "done_reason": "stop"})(),
        ]
    )

    engine = GrowthEngine("test_story", db, router, _make_cfg(), llm_provider="ollama", llm_model="test")

    await engine.process_round(8, [_make_char()])

    db.insert_character_growth_candidate.assert_not_awaited()
    db.insert_evolution.assert_not_awaited()
    db.upsert_character_profile_overlay.assert_not_awaited()
    db.insert_generation_quality_issue.assert_awaited_once()
    result = engine.get_last_round_results()["char_a"]
    assert result["skip_reason"] == "parse_failed"


@async_to_sync
async def test_growth_engine_uses_growth_batch_and_reports_rollback() -> None:
    db = AsyncMock()
    db.get_relevant_story_memories = AsyncMock(
        return_value=[{"id": 1, "summary": "約束を果たした。", "importance": 0.9}]
    )
    db.get_relevant_story_hooks = AsyncMock(return_value=[])
    db.get_recent_relationship_events_for_character = AsyncMock(return_value=[])
    db.get_active_tensions = AsyncMock(return_value=[])
    db.get_recent_closed_story_scenes = AsyncMock(return_value=[])
    db.get_character_profile_overlay = AsyncMock(return_value=None)
    db.get_latest_evolution_overlay = AsyncMock(return_value={})
    db.get_pending_growth_candidates = AsyncMock(return_value=[])
    db.apply_growth_batch = AsyncMock(side_effect=RuntimeError("boom"))
    db.insert_generation_quality_issue = AsyncMock()
    router = AsyncMock()
    router.generate = AsyncMock(
        return_value=type(
            "Resp",
            (),
            {
                "text": """
                [
                  {
                    "field": "current_goal",
                    "candidate_value": "もう逃げない",
                    "reason": "重要な約束を果たした",
                    "experience_score": 0.92,
                    "identity_impact_score": 0.9,
                    "confidence": 0.82,
                    "source_memory_id": 1
                  }
                ]
                """,
                "done_reason": "stop",
            },
        )()
    )

    engine = GrowthEngine("test_story", db, router, _make_cfg(), llm_provider="ollama", llm_model="test")

    await engine.process_round(8, [_make_char()])

    db.apply_growth_batch.assert_awaited_once()
    db.insert_generation_quality_issue.assert_awaited_once()
    result = engine.get_last_round_results()["char_a"]
    assert result["skip_reason"] == "write_rolled_back"


@async_to_sync
async def test_growth_engine_includes_active_tensions_and_closed_scenes_in_evidence_prompt() -> None:
    db = AsyncMock()
    db.get_relevant_story_memories = AsyncMock(return_value=[])
    db.get_relevant_story_hooks = AsyncMock(return_value=[])
    db.get_recent_relationship_events_for_character = AsyncMock(return_value=[])
    db.get_active_tensions = AsyncMock(
        return_value=[
            {
                "id": 5,
                "tension_type": "conflict",
                "description": "まだ解けていない対立がある。",
                "involved_chars": ["char_a", "char_b"],
            }
        ]
    )
    db.get_recent_closed_story_scenes = AsyncMock(
        return_value=[
            {
                "id": 9,
                "scene_type": "conversation",
                "outcome_summary": "放課後に続きを話す約束が残った。",
            }
        ]
    )
    db.get_character_profile_overlay = AsyncMock(return_value=None)
    db.get_latest_evolution_overlay = AsyncMock(return_value={})
    router = AsyncMock()
    router.generate = AsyncMock(
        return_value=type(
            "Resp",
            (),
            {
                "text": "[]",
                "done_reason": "stop",
            },
        )()
    )

    engine = GrowthEngine("test_story", db, router, _make_cfg(), llm_provider="ollama", llm_model="test")

    await engine.process_round(12, [_make_char()])

    user_prompt = router.generate.await_args_list[0].kwargs["user_prompt"]
    assert "まだ解けていない対立がある。" in user_prompt
    assert "放課後に続きを話す約束が残った。" in user_prompt


@async_to_sync
async def test_growth_engine_builds_prompt_from_effective_profile_and_richer_live_evidence() -> None:
    db = AsyncMock()
    db.get_relevant_story_memories = AsyncMock(return_value=[])
    db.get_relevant_story_hooks = AsyncMock(return_value=[])
    db.get_recent_relationship_events_for_character = AsyncMock(return_value=[])
    db.get_active_tensions = AsyncMock(return_value=[])
    db.get_recent_closed_story_scenes = AsyncMock(return_value=[])
    db.get_character_profile_overlay = AsyncMock(return_value=None)
    db.get_latest_evolution_overlay = AsyncMock(return_value={})
    db.get_character_canon_overlay = AsyncMock(
        return_value={"overlay_json": {"current_worry": "秘密が漏れるのが怖い"}}
    )
    db.get_active_story_episode = AsyncMock(
        return_value={
            "id": 7,
            "episode_type": "status_clash",
            "goal": "張り合いに決着をつける",
            "focus_char_ids": ["char_a", "char_b"],
        }
    )
    db.get_active_relationship_modes = AsyncMock(
        return_value=[
            {
                "char_id_from": "char_a",
                "char_id_to": "char_b",
                "mode_type": "irritated_respect",
                "summary": "認めつつ張り合っている",
                "intensity": 0.9,
                "confidence": 0.8,
            }
        ]
    )
    db.get_active_story_canon_bits = AsyncMock(
        return_value=[
            {
                "id": 3,
                "motif_key": "status_clash",
                "canon_level": "proto_canon",
                "summary": "この二人は張り合いに戻りやすい",
                "focus_char_ids": ["char_a", "char_b"],
            }
        ]
    )
    db.get_active_story_dramatic_pressures = AsyncMock(
        return_value=[
            {
                "id": 5,
                "pressure_type": "status_flashpoint",
                "summary": "張り合いに触れると場が動く",
                "score": 0.88,
                "focus_char_ids": ["char_a", "char_b"],
            }
        ]
    )
    router = AsyncMock()
    router.generate = AsyncMock(
        return_value=type(
            "Resp",
            (),
            {
                "text": "[]",
                "done_reason": "stop",
            },
        )()
    )

    engine = GrowthEngine("test_story", db, router, _make_cfg(), llm_provider="ollama", llm_model="test")

    await engine.process_round(14, [_make_char()])

    user_prompt = router.generate.await_args_list[0].kwargs["user_prompt"]
    assert "秘密が漏れるのが怖い" in user_prompt
    assert "張り合いに決着をつける" in user_prompt
    assert "認めつつ張り合っている" in user_prompt
    assert "この二人は張り合いに戻りやすい" in user_prompt
    assert "張り合いに触れると場が動く" in user_prompt


@async_to_sync
async def test_growth_engine_compacts_prompt_and_budget_for_gemma() -> None:
    db = AsyncMock()
    db.get_relevant_story_memories = AsyncMock(
        return_value=[{"id": 1, "summary": "放課後の廊下で、まだ整理しきれない勝ち負けの記憶が刺さった。", "importance": 0.8}]
    )
    db.get_relevant_story_hooks = AsyncMock(
        return_value=[{"id": 2, "description": "次の一言で関係が決まりそうな火種が残っている。"}]
    )
    db.get_recent_relationship_events_for_character = AsyncMock(
        return_value=[{"summary": "認めつつも順番を譲らない空気が続いている。", "source_log_id": 9, "scene_id": 4}]
    )
    db.get_active_tensions = AsyncMock(
        return_value=[{"id": 5, "description": "誰が主導権を取るかの対立がまだ続いている。", "involved_chars": ["char_a"]}]
    )
    db.get_recent_closed_story_scenes = AsyncMock(
        return_value=[{"id": 9, "outcome_summary": "見せ場の順番だけが未決のまま残った。"}]
    )
    db.get_character_profile_overlay = AsyncMock(return_value=None)
    db.get_latest_evolution_overlay = AsyncMock(return_value={})
    db.get_character_canon_overlay = AsyncMock(return_value=None)
    db.get_active_story_episode = AsyncMock(return_value={"id": 7, "goal": "張り合いに決着をつける"})
    db.get_active_relationship_modes = AsyncMock(return_value=[])
    db.get_active_story_canon_bits = AsyncMock(return_value=[])
    db.get_active_story_dramatic_pressures = AsyncMock(return_value=[])
    router = AsyncMock()
    router.generate = AsyncMock(
        return_value=type("Resp", (), {"text": "[]", "done_reason": "stop"})()
    )

    engine = GrowthEngine("test_story", db, router, _make_cfg(), llm_provider="ollama", llm_model="gemma4:e4b")

    await engine.process_round(14, [_make_char()])

    first_kwargs = router.generate.await_args_list[0].kwargs
    assert first_kwargs["max_tokens"] == 220
    assert len(first_kwargs["user_prompt"]) < 420
    assert "JSON配列のみ。最大1件。generic禁止。" in first_kwargs["user_prompt"]


@async_to_sync
async def test_growth_engine_rejects_generic_positive_candidate_before_insert() -> None:
    db = AsyncMock()
    db.get_relevant_story_memories = AsyncMock(
        return_value=[{"id": 1, "summary": "大きな約束を果たした。", "importance": 0.9}]
    )
    db.get_relevant_story_hooks = AsyncMock(return_value=[])
    db.get_recent_relationship_events_for_character = AsyncMock(return_value=[])
    db.get_active_tensions = AsyncMock(return_value=[])
    db.get_recent_closed_story_scenes = AsyncMock(return_value=[])
    db.get_character_profile_overlay = AsyncMock(return_value=None)
    db.get_latest_evolution_overlay = AsyncMock(return_value={})
    db.get_character_canon_overlay = AsyncMock(return_value=None)
    db.get_active_story_episode = AsyncMock(return_value=None)
    db.get_active_relationship_modes = AsyncMock(return_value=[])
    db.get_active_story_canon_bits = AsyncMock(return_value=[])
    db.get_active_story_dramatic_pressures = AsyncMock(return_value=[])
    db.insert_character_growth_candidate = AsyncMock()
    db.insert_evolution = AsyncMock()
    db.upsert_character_profile_overlay = AsyncMock()
    db.insert_generation_quality_issue = AsyncMock()
    router = AsyncMock()
    router.generate = AsyncMock(
        return_value=type(
            "Resp",
            (),
            {
                "text": """
                [
                  {
                    "field": "current_goal",
                    "candidate_value": "もっと前向きになる",
                    "reason": "約束を果たして成長した",
                    "experience_score": 0.88,
                    "identity_impact_score": 0.82,
                    "confidence": 0.84,
                    "source_memory_id": 1
                  }
                ]
                """,
                "done_reason": "stop",
            },
        )()
    )

    engine = GrowthEngine("test_story", db, router, _make_cfg(), llm_provider="ollama", llm_model="test")

    await engine.process_round(14, [_make_char()])

    db.insert_character_growth_candidate.assert_not_awaited()
    db.insert_evolution.assert_not_awaited()
    db.upsert_character_profile_overlay.assert_not_awaited()
    db.insert_generation_quality_issue.assert_awaited()
    issue_types = [call.args[1]["issue_type"] for call in db.insert_generation_quality_issue.await_args_list]
    assert "growth_generic_rejected" in issue_types


@async_to_sync
async def test_growth_engine_rejects_transient_personality_core_without_durable_support() -> None:
    db = AsyncMock()
    db.get_relevant_story_memories = AsyncMock(
        return_value=[{"id": 1, "summary": "背中を押されて少し安心した。", "importance": 0.6}]
    )
    db.get_relevant_story_hooks = AsyncMock(return_value=[])
    db.get_recent_relationship_events_for_character = AsyncMock(return_value=[])
    db.get_active_tensions = AsyncMock(return_value=[])
    db.get_recent_closed_story_scenes = AsyncMock(return_value=[])
    db.get_character_profile_overlay = AsyncMock(return_value=None)
    db.get_latest_evolution_overlay = AsyncMock(return_value={})
    db.get_character_canon_overlay = AsyncMock(return_value=None)
    db.get_active_story_episode = AsyncMock(return_value=None)
    db.get_active_relationship_modes = AsyncMock(return_value=[])
    db.get_active_story_canon_bits = AsyncMock(return_value=[])
    db.get_active_story_dramatic_pressures = AsyncMock(return_value=[])
    db.insert_character_growth_candidate = AsyncMock()
    db.insert_evolution = AsyncMock()
    db.upsert_character_profile_overlay = AsyncMock()
    db.insert_generation_quality_issue = AsyncMock()
    router = AsyncMock()
    router.generate = AsyncMock(
        return_value=type(
            "Resp",
            (),
            {
                "text": """
                [
                  {
                    "field": "personality_core",
                    "candidate_value": "少し前向きになる",
                    "reason": "励まされて少し気が楽になった",
                    "experience_score": 0.75,
                    "identity_impact_score": 0.78,
                    "confidence": 0.82,
                    "source_memory_id": 1
                  }
                ]
                """,
                "done_reason": "stop",
            },
        )()
    )

    engine = GrowthEngine("test_story", db, router, _make_cfg(), llm_provider="ollama", llm_model="test")

    await engine.process_round(14, [_make_char()])

    db.insert_character_growth_candidate.assert_not_awaited()
    db.insert_evolution.assert_not_awaited()
    db.insert_generation_quality_issue.assert_awaited()
    issue_types = [call.args[1]["issue_type"] for call in db.insert_generation_quality_issue.await_args_list]
    assert "growth_low_quality_rejected" in issue_types


@async_to_sync
async def test_growth_engine_records_empty_result_when_rich_evidence_yields_no_candidate() -> None:
    db = AsyncMock()
    db.get_relevant_story_memories = AsyncMock(
        return_value=[{"id": 1, "summary": "危うい秘密を抱えた。", "importance": 0.8}]
    )
    db.get_relevant_story_hooks = AsyncMock(
        return_value=[{"id": 2, "description": "まだ言えていない秘密がある。"}]
    )
    db.get_recent_relationship_events_for_character = AsyncMock(
        return_value=[{"source_log_id": 9, "scene_id": 4, "summary": "一人にだけ本音を漏らした。"}]
    )
    db.get_active_tensions = AsyncMock(
        return_value=[
            {
                "id": 5,
                "description": "秘密がまだ場に残っている。",
                "involved_chars": ["char_a", "char_b"],
            }
        ]
    )
    db.get_recent_closed_story_scenes = AsyncMock(
        return_value=[{"id": 7, "scene_type": "conversation", "outcome_summary": "本題はまだ曖昧なまま終わった。"}]
    )
    db.get_character_profile_overlay = AsyncMock(return_value=None)
    db.get_latest_evolution_overlay = AsyncMock(return_value={})
    db.get_character_canon_overlay = AsyncMock(
        return_value={"overlay_json": {"current_worry": "秘密が漏れるのが怖い"}}
    )
    db.get_active_story_episode = AsyncMock(
        return_value={"id": 3, "episode_type": "near_reveal", "goal": "曖昧なままにしない"}
    )
    db.get_active_relationship_modes = AsyncMock(
        return_value=[
            {
                "char_id_from": "char_a",
                "char_id_to": "char_b",
                "mode_type": "unsafe_confidant",
                "summary": "危うい本音を漏らしやすい",
                "intensity": 0.82,
                "confidence": 0.8,
            }
        ]
    )
    db.get_active_story_canon_bits = AsyncMock(
        return_value=[
            {
                "id": 11,
                "motif_key": "near_reveal",
                "canon_level": "canon",
                "summary": "秘密が曖昧なまま残りやすい",
                "focus_char_ids": ["char_a", "char_b"],
            }
        ]
    )
    db.get_active_story_dramatic_pressures = AsyncMock(
        return_value=[
            {
                "id": 21,
                "pressure_type": "near_reveal",
                "summary": "言い切られていないことに触れると場が動く",
                "score": 0.91,
                "focus_char_ids": ["char_a", "char_b"],
            }
        ]
    )
    db.insert_character_growth_candidate = AsyncMock()
    db.insert_evolution = AsyncMock()
    db.upsert_character_profile_overlay = AsyncMock()
    db.insert_generation_quality_issue = AsyncMock()
    router = AsyncMock()
    router.generate = AsyncMock(
        return_value=type(
            "Resp",
            (),
            {
                "text": "[]",
                "done_reason": "stop",
            },
        )()
    )

    engine = GrowthEngine("test_story", db, router, _make_cfg(), llm_provider="ollama", llm_model="test")

    await engine.process_round(14, [_make_char()])

    db.insert_character_growth_candidate.assert_not_awaited()
    db.insert_evolution.assert_not_awaited()
    db.insert_generation_quality_issue.assert_awaited()
    issue_types = [call.args[1]["issue_type"] for call in db.insert_generation_quality_issue.await_args_list]
    assert "growth_llm_empty" in issue_types


@async_to_sync
async def test_growth_engine_current_worry_commits_at_lower_aggregate_threshold() -> None:
    db = AsyncMock()
    db.get_relevant_story_memories = AsyncMock(return_value=[])
    db.get_relevant_story_hooks = AsyncMock(
        return_value=[{"id": 2, "description": "まだ言えていない秘密がある。"}]
    )
    db.get_recent_relationship_events_for_character = AsyncMock(return_value=[])
    db.get_active_tensions = AsyncMock(return_value=[])
    db.get_recent_closed_story_scenes = AsyncMock(return_value=[])
    db.get_character_profile_overlay = AsyncMock(return_value=None)
    db.get_latest_evolution_overlay = AsyncMock(return_value={})
    db.get_character_canon_overlay = AsyncMock(
        return_value={"overlay_json": {"current_worry": "まだ曖昧なまま終わるのが怖い"}}
    )
    db.get_active_story_episode = AsyncMock(
        return_value={
            "id": 3,
            "episode_type": "near_reveal",
            "goal": "曖昧なままにしない",
            "focus_char_ids": ["char_a", "char_b"],
        }
    )
    db.get_active_relationship_modes = AsyncMock(
        return_value=[
            {
                "char_id_from": "char_a",
                "char_id_to": "char_b",
                "mode_type": "unsafe_confidant",
                "summary": "危うい本音を漏らしやすい",
                "intensity": 0.8,
                "confidence": 0.8,
            }
        ]
    )
    db.get_active_story_canon_bits = AsyncMock(return_value=[])
    db.get_active_story_dramatic_pressures = AsyncMock(return_value=[])
    db.insert_character_growth_candidate = AsyncMock(return_value=31)
    db.get_pending_growth_candidates = AsyncMock(
        return_value=[
            {
                "id": 9,
                "field": "current_worry",
                "candidate_value": "秘密を曖昧なまま流したくない",
                "experience_score": 0.7,
                "confidence": 0.72,
                "detected_turn": 10,
                "status": "pending",
            }
        ]
    )
    db.insert_evolution = AsyncMock(return_value=41)
    db.upsert_character_profile_overlay = AsyncMock()
    db.update_character_growth_candidate_status = AsyncMock()
    router = AsyncMock()
    router.generate = AsyncMock(
        return_value=type(
            "Resp",
            (),
            {
                "text": """
                [
                  {
                    "field": "current_worry",
                    "candidate_value": "秘密を曖昧なまま流したくない",
                    "reason": "この件を曖昧なまま終わらせたくない",
                    "experience_score": 0.78,
                    "identity_impact_score": 0.62,
                    "confidence": 0.68,
                    "source_hook_id": 2
                  }
                ]
                """,
                "done_reason": "stop",
            },
        )()
    )

    engine = GrowthEngine("test_story", db, router, _make_cfg(), llm_provider="ollama", llm_model="test")

    await engine.process_round(14, [_make_char()])

    db.insert_evolution.assert_awaited_once()
    committed_ids = [call.args[0] for call in db.update_character_growth_candidate_status.await_args_list]
    assert committed_ids == [31, 9]


@async_to_sync
async def test_growth_engine_current_goal_commits_with_single_quality_candidate() -> None:
    db = AsyncMock()
    db.get_relevant_story_memories = AsyncMock(return_value=[])
    db.get_relevant_story_hooks = AsyncMock(
        return_value=[{"id": 2, "description": "張り合いに決着をつけたい。"}]
    )
    db.get_recent_relationship_events_for_character = AsyncMock(return_value=[])
    db.get_active_tensions = AsyncMock(return_value=[])
    db.get_recent_closed_story_scenes = AsyncMock(return_value=[])
    db.get_character_profile_overlay = AsyncMock(return_value=None)
    db.get_latest_evolution_overlay = AsyncMock(return_value={})
    db.get_character_canon_overlay = AsyncMock(return_value=None)
    db.get_active_story_episode = AsyncMock(
        return_value={
            "id": 3,
            "episode_type": "status_clash",
            "goal": "張り合いに決着をつける",
            "focus_char_ids": ["char_a", "char_b"],
        }
    )
    db.get_active_relationship_modes = AsyncMock(
        return_value=[
            {
                "char_id_from": "char_a",
                "char_id_to": "char_b",
                "mode_type": "irritated_respect",
                "summary": "認めつつ張り合っている",
                "intensity": 0.8,
                "confidence": 0.8,
            }
        ]
    )
    db.get_active_story_canon_bits = AsyncMock(return_value=[])
    db.get_active_story_dramatic_pressures = AsyncMock(return_value=[])
    db.insert_character_growth_candidate = AsyncMock(return_value=32)
    db.get_pending_growth_candidates = AsyncMock(return_value=[])
    db.insert_evolution = AsyncMock(return_value=42)
    db.upsert_character_profile_overlay = AsyncMock()
    db.update_character_growth_candidate_status = AsyncMock()
    router = AsyncMock()
    router.generate = AsyncMock(
        return_value=type(
            "Resp",
            (),
            {
                "text": """
                [
                  {
                    "field": "current_goal",
                    "candidate_value": "張り合いに決着をつけたい",
                    "reason": "この張り合いを引き分けのままにしたくない",
                    "experience_score": 0.9,
                    "identity_impact_score": 0.7,
                    "confidence": 0.8,
                    "source_hook_id": 2
                  }
                ]
                """,
                "done_reason": "stop",
            },
        )()
    )

    engine = GrowthEngine("test_story", db, router, _make_cfg(), llm_provider="ollama", llm_model="test")

    await engine.process_round(14, [_make_char()])

    db.insert_evolution.assert_awaited_once()
    db.update_character_growth_candidate_status.assert_awaited_once_with(32, "committed")


@async_to_sync
async def test_growth_engine_personality_core_still_rejects_single_durable_source() -> None:
    db = AsyncMock()
    db.get_relevant_story_memories = AsyncMock(return_value=[])
    db.get_relevant_story_hooks = AsyncMock(return_value=[])
    db.get_recent_relationship_events_for_character = AsyncMock(return_value=[])
    db.get_active_tensions = AsyncMock(return_value=[])
    db.get_recent_closed_story_scenes = AsyncMock(return_value=[])
    db.get_character_profile_overlay = AsyncMock(return_value=None)
    db.get_latest_evolution_overlay = AsyncMock(return_value={})
    db.get_character_canon_overlay = AsyncMock(return_value=None)
    db.get_active_story_episode = AsyncMock(
        return_value={
            "id": 3,
            "episode_type": "near_reveal",
            "goal": "曖昧なままにしない",
            "focus_char_ids": ["char_a", "char_b"],
        }
    )
    db.get_active_relationship_modes = AsyncMock(return_value=[])
    db.get_active_story_canon_bits = AsyncMock(return_value=[])
    db.get_active_story_dramatic_pressures = AsyncMock(return_value=[])
    db.insert_character_growth_candidate = AsyncMock()
    db.insert_evolution = AsyncMock()
    db.upsert_character_profile_overlay = AsyncMock()
    db.insert_generation_quality_issue = AsyncMock()
    router = AsyncMock()
    router.generate = AsyncMock(
        return_value=type(
            "Resp",
            (),
            {
                "text": """
                [
                  {
                    "field": "personality_core",
                    "candidate_value": "曖昧さを放置しにくい",
                    "reason": "今回は曖昧なまま終わらせたくないと感じた",
                    "experience_score": 0.82,
                    "identity_impact_score": 0.74,
                    "confidence": 0.82
                  }
                ]
                """,
                "done_reason": "stop",
            },
        )()
    )

    engine = GrowthEngine("test_story", db, router, _make_cfg(), llm_provider="ollama", llm_model="test")

    await engine.process_round(14, [_make_char()])

    db.insert_character_growth_candidate.assert_not_awaited()
    db.insert_evolution.assert_not_awaited()
    issue_types = [call.args[1]["issue_type"] for call in db.insert_generation_quality_issue.await_args_list]
    assert "growth_low_quality_rejected" in issue_types


@async_to_sync
async def test_growth_engine_expires_stale_pending_candidate_before_matching_threshold() -> None:
    db = AsyncMock()
    db.get_relevant_story_memories = AsyncMock(
        return_value=[{"id": 1, "summary": "一度だけ背中を押された。", "importance": 0.6}]
    )
    db.get_relevant_story_hooks = AsyncMock(return_value=[])
    db.get_recent_relationship_events_for_character = AsyncMock(return_value=[])
    db.get_active_tensions = AsyncMock(return_value=[])
    db.get_recent_closed_story_scenes = AsyncMock(return_value=[])
    db.get_character_profile_overlay = AsyncMock(return_value=None)
    db.get_latest_evolution_overlay = AsyncMock(return_value={"current_goal": "様子を見る"})
    db.get_character_canon_overlay = AsyncMock(return_value=None)
    db.get_active_story_episode = AsyncMock(return_value=None)
    db.get_active_relationship_modes = AsyncMock(return_value=[])
    db.get_active_story_canon_bits = AsyncMock(return_value=[])
    db.get_active_story_dramatic_pressures = AsyncMock(return_value=[])
    db.insert_character_growth_candidate = AsyncMock(return_value=51)
    db.get_pending_growth_candidates = AsyncMock(
        return_value=[
            {
                "id": 8,
                "field": "current_goal",
                "candidate_value": "踏み出す",
                "experience_score": 0.95,
                "confidence": 0.9,
                "detected_turn": 1,
                "status": "pending",
            }
        ]
    )
    db.insert_evolution = AsyncMock()
    db.upsert_character_profile_overlay = AsyncMock()
    db.update_character_growth_candidate_status = AsyncMock()
    router = AsyncMock()
    router.generate = AsyncMock(
        return_value=type(
            "Resp",
            (),
            {
                "text": """
                [
                  {
                    "field": "current_goal",
                    "candidate_value": "踏み出す",
                    "reason": "一歩だけ前に出たいと感じた",
                    "experience_score": 0.5,
                    "identity_impact_score": 0.6,
                    "confidence": 0.6,
                    "source_memory_id": 1
                  }
                ]
                """,
                "done_reason": "stop",
            },
        )()
    )

    engine = GrowthEngine(
        "test_story",
        db,
        router,
        _make_cfg(pending_candidate_ttl_turns=4),
        llm_provider="ollama",
        llm_model="test",
    )

    await engine.process_round(10, [_make_char()])

    db.insert_evolution.assert_not_awaited()
    db.update_character_growth_candidate_status.assert_awaited_once_with(8, "expired")
    result = engine.get_last_round_results()["char_a"]
    assert result["result"] == "pending_candidate_inserted"


@async_to_sync
async def test_growth_engine_supersedes_conflicting_pending_candidate_when_new_candidate_is_inserted() -> None:
    db = AsyncMock()
    db.get_relevant_story_memories = AsyncMock(
        return_value=[{"id": 1, "summary": "一度だけ背中を押された。", "importance": 0.6}]
    )
    db.get_relevant_story_hooks = AsyncMock(return_value=[])
    db.get_recent_relationship_events_for_character = AsyncMock(return_value=[])
    db.get_active_tensions = AsyncMock(return_value=[])
    db.get_recent_closed_story_scenes = AsyncMock(return_value=[])
    db.get_character_profile_overlay = AsyncMock(return_value=None)
    db.get_latest_evolution_overlay = AsyncMock(return_value={"current_goal": "様子を見る"})
    db.get_character_canon_overlay = AsyncMock(return_value=None)
    db.get_active_story_episode = AsyncMock(return_value=None)
    db.get_active_relationship_modes = AsyncMock(return_value=[])
    db.get_active_story_canon_bits = AsyncMock(return_value=[])
    db.get_active_story_dramatic_pressures = AsyncMock(return_value=[])
    db.insert_character_growth_candidate = AsyncMock(return_value=61)
    db.get_pending_growth_candidates = AsyncMock(
        return_value=[
            {
                "id": 9,
                "field": "current_goal",
                "candidate_value": "保留する",
                "experience_score": 0.6,
                "confidence": 0.6,
                "detected_turn": 9,
                "status": "pending",
            }
        ]
    )
    db.insert_evolution = AsyncMock()
    db.upsert_character_profile_overlay = AsyncMock()
    db.update_character_growth_candidate_status = AsyncMock()
    router = AsyncMock()
    router.generate = AsyncMock(
        return_value=type(
            "Resp",
            (),
            {
                "text": """
                [
                  {
                    "field": "current_goal",
                    "candidate_value": "踏み出す",
                    "reason": "一歩だけ前に出たいと感じた",
                    "experience_score": 0.5,
                    "identity_impact_score": 0.6,
                    "confidence": 0.6,
                    "source_memory_id": 1
                  }
                ]
                """,
                "done_reason": "stop",
            },
        )()
    )

    engine = GrowthEngine("test_story", db, router, _make_cfg(), llm_provider="ollama", llm_model="test")

    await engine.process_round(10, [_make_char()])

    db.insert_evolution.assert_not_awaited()
    updated = [(call.args[0], call.args[1]) for call in db.update_character_growth_candidate_status.await_args_list]
    assert (9, "superseded") in updated


# ============================================================
# Growth Commit 安定性向上: parse / validation 改善テスト
# ============================================================


def test_validate_growth_candidate_accepts_long_phrase_with_embedded_cue() -> None:
    """15文字以上の具体的な goal に汎用 cue ('成長') が埋まっていても accept されること。"""
    char = {"id": "char_a", "current_goal": "日々の練習", "current_worry": "時間不足", "personality_core": "冷静"}
    overlay: dict = {}
    candidate = {
        "field": "current_goal",
        "candidate_value": "成長した経験を活かして部活を引っ張る存在になる",  # 26文字、'成長'は冒頭
        "reason": "部長として振る舞い始めた",
        "experience_score": 0.7,
        "identity_impact_score": 0.6,
        "confidence": 0.7,
    }
    # '成長' が冒頭8文字以内（位置0）なので依然 reject
    result = _validate_growth_candidate(
        char=char, overlay=overlay, candidate=candidate,
        durable_support_count=2, personality_core_min_durable_support=2,
    )
    assert result == "growth_generic_rejected"

    # 冒頭8文字以降に cue が現れる長い value は accept
    candidate2 = dict(candidate, candidate_value="部活を引っ張る存在になりたいと成長している")
    result2 = _validate_growth_candidate(
        char=char, overlay=overlay, candidate=candidate2,
        durable_support_count=2, personality_core_min_durable_support=2,
    )
    assert result2 is None


def test_validate_growth_candidate_rejects_short_generic_goal() -> None:
    """15文字未満の汎用的な goal 値（'前向きになる'）は reject されること。"""
    char = {"id": "char_a", "current_goal": "日々の練習", "current_worry": "時間不足", "personality_core": "冷静"}
    overlay: dict = {}
    candidate = {
        "field": "current_goal",
        "candidate_value": "前向きになる",  # 短い汎用表現
        "reason": "気持ちが変わった",
        "experience_score": 0.5,
        "identity_impact_score": 0.4,
        "confidence": 0.5,
    }
    result = _validate_growth_candidate(
        char=char, overlay=overlay, candidate=candidate,
        durable_support_count=2, personality_core_min_durable_support=2,
    )
    assert result == "growth_generic_rejected"


def test_try_parse_truncated_array_recovers_first_object() -> None:
    """配列が途中で truncate された JSON から最初の完全 object だけ回収できること。"""
    # 2件目の object が途中で切れたケース
    truncated = '[{"field": "current_goal", "candidate_value": "音楽で仲間を導く", "reason": "部長として覚悟した", "experience_score": 0.8, "identity_impact_score": 0.7, "confidence": 0.75}, {"field": "personality_core", "candidate_value": "強い'
    fragments = _candidate_growth_payload_fragments(truncated)
    # いずれかの fragment から最初の完全 object を含む "[{...}]" が生成されていること
    valid_fragment = next(
        (f for f in fragments if f.startswith("[{") and f.endswith("}]")),
        None,
    )
    assert valid_fragment is not None
    import json
    parsed = json.loads(valid_fragment)
    assert isinstance(parsed, list)
    assert parsed[0]["field"] == "current_goal"

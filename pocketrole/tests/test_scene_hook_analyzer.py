"""Tests for engine/scene_hook_analyzer.py."""

from __future__ import annotations

from unittest.mock import AsyncMock

from engine.scene_hook_analyzer import SceneHookAnalyzer, _parse_scene_hook_result
from tests._async_harness import async_to_sync


def test_parse_scene_hook_result_filters_invalid_hook_ids() -> None:
    text = """
    {
      "refined_outcome_summary": "約束だけが残った。",
      "resolve_hook_ids": [3, 999],
      "keep_hook_ids": [4],
      "merged_hooks": [
        {
          "hook_type": "promise",
          "title": "放課後の約束",
          "description": "放課後に屋上で会う約束が一本化された。",
          "priority": 0.82
        }
      ]
    }
    """

    parsed = _parse_scene_hook_result(text, valid_hook_ids={3, 4})

    assert parsed is not None
    assert parsed["resolve_hook_ids"] == [3]
    assert parsed["keep_hook_ids"] == [4]
    assert parsed["merged_hooks"][0]["hook_type"] == "promise"


@async_to_sync
async def test_scene_hook_analyzer_skips_llm_without_open_hooks() -> None:
    db = AsyncMock()
    db.get_story_scene = AsyncMock(
        return_value={"id": 9, "scene_type": "conversation", "outcome_summary": "参加者が分散した。"}
    )
    db.get_scene_logs = AsyncMock(return_value=[{"id": 10, "message": "また今度。"}])
    db.get_open_story_hooks_for_scene = AsyncMock(return_value=[])
    router = AsyncMock()
    analyzer = SceneHookAnalyzer("test_story", db, router, llm_provider="ollama", llm_model="test")

    result = await analyzer.analyze_closed_scene(9)

    assert result is None
    router.generate.assert_not_awaited()


@async_to_sync
async def test_scene_hook_analyzer_sets_request_tag_and_gemma_budget() -> None:
    db = AsyncMock()
    db.get_story_scene = AsyncMock(
        return_value={"id": 9, "scene_type": "conversation", "outcome_summary": "かなり長い結末の説明 " * 10}
    )
    db.get_scene_logs = AsyncMock(
        return_value=[
            {"id": idx, "char_id": f"char_{idx}", "message": "長い会話断片 " * 12}
            for idx in range(8)
        ]
    )
    db.get_open_story_hooks_for_scene = AsyncMock(
        return_value=[
            {"id": 3, "hook_type": "promise", "description": "長い約束の説明 " * 10},
        ]
    )
    router = AsyncMock()
    router.generate = AsyncMock(return_value=AsyncMock(text='{"keep_hook_ids":[3]}'))
    analyzer = SceneHookAnalyzer("test_story", db, router, llm_provider="ollama", llm_model="gemma4:e4b")

    await analyzer.analyze_closed_scene(9)

    kwargs = router.generate.await_args.kwargs
    assert kwargs["request_tag"] == "scene_hook_analyzer:scene_9"
    assert kwargs["max_tokens"] == 240
    assert kwargs["temperature"] == 0.0
    assert kwargs["reasoning_mode"] == "off"
    assert len(kwargs["user_prompt"]) < 900

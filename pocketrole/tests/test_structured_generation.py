"""Tests for engine/structured_generation.py."""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock

from engine.llm.base import LLMResponse
from engine.structured_generation import (
    StructuredJSONPolicy,
    generate_structured_json,
)
from tests._async_harness import async_to_sync


def _make_response(text: str, *, done_reason: str = "stop") -> LLMResponse:
    return LLMResponse(
        text=text,
        model="test_model",
        provider="ollama",
        prompt_tokens=10,
        completion_tokens=20,
        latency_ms=100,
        done_reason=done_reason,
    )


def _parse_object(text: str) -> dict | None:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


@async_to_sync
async def test_generate_structured_json_repairs_length_truncated_primary_response() -> None:
    router = MagicMock()
    router.generate = AsyncMock(
        side_effect=[
            _make_response('{"summary":"broken"', done_reason="length"),
            _make_response('{"summary":"fixed"}'),
        ]
    )

    result = await generate_structured_json(
        router=router,
        provider="ollama",
        model="test_model",
        system_prompt="system",
        user_prompt="user",
        parser=_parse_object,
        repair_schema_prompt='{"summary":"..."}',
        policy=StructuredJSONPolicy(
            name="story_memory",
            response_max_tokens=220,
            repair_max_tokens=320,
        ),
    )

    assert result.parsed == {"summary": "fixed"}
    assert result.used_repair is True
    assert result.done_reason == "length"
    assert router.generate.await_count == 2


@async_to_sync
async def test_generate_structured_json_returns_none_when_repair_also_fails() -> None:
    router = MagicMock()
    router.generate = AsyncMock(
        side_effect=[
            _make_response('{"summary":"broken"', done_reason="length"),
            _make_response("still not json"),
        ]
    )

    result = await generate_structured_json(
        router=router,
        provider="ollama",
        model="test_model",
        system_prompt="system",
        user_prompt="user",
        parser=_parse_object,
        repair_schema_prompt='{"summary":"..."}',
        policy=StructuredJSONPolicy(
            name="story_memory",
            response_max_tokens=220,
            repair_max_tokens=320,
        ),
    )

    assert result.parsed is None
    assert result.used_repair is True
    assert router.generate.await_count == 2


@async_to_sync
async def test_generate_structured_json_captures_parser_exceptions() -> None:
    router = MagicMock()
    router.generate = AsyncMock(return_value=_make_response('{"summary":"ok"}'))

    def _raising_parser(_: str) -> dict | None:
        raise ValueError("bad score token")

    result = await generate_structured_json(
        router=router,
        provider="ollama",
        model="test_model",
        system_prompt="system",
        user_prompt="user",
        parser=_raising_parser,
        repair_schema_prompt='{"summary":"..."}',
        policy=StructuredJSONPolicy(
            name="story_memory",
            response_max_tokens=220,
            repair_max_tokens=320,
        ),
    )

    assert result.parsed is None
    assert result.used_repair is True
    assert result.parser_error == "bad score token"
    assert router.generate.await_count == 2


@async_to_sync
async def test_generate_structured_json_repair_prompt_mentions_bracket_and_comma_repair() -> None:
    router = MagicMock()
    router.generate = AsyncMock(
        side_effect=[
            _make_response('[{"summary":"broken"', done_reason="length"),
            _make_response('{"summary":"fixed"}'),
        ]
    )

    await generate_structured_json(
        router=router,
        provider="ollama",
        model="test_model",
        system_prompt="system",
        user_prompt="user",
        parser=_parse_object,
        repair_schema_prompt='{"summary":"..."}',
        policy=StructuredJSONPolicy(
            name="story_memory",
            response_max_tokens=220,
            repair_max_tokens=320,
        ),
    )

    repair_kwargs = router.generate.await_args_list[1].kwargs
    assert "末尾カンマ" in repair_kwargs["system_prompt"]
    assert "閉じ括弧" in repair_kwargs["user_prompt"]


@async_to_sync
async def test_generate_structured_json_repair_prompt_enforces_array_only_contract() -> None:
    router = MagicMock()
    router.generate = AsyncMock(
        side_effect=[
            _make_response("候補です\n- current_goal\n[{\"field\":\"current_goal\"}", done_reason="length"),
            _make_response("[]"),
        ]
    )

    await generate_structured_json(
        router=router,
        provider="ollama",
        model="test_model",
        system_prompt="system",
        user_prompt="user",
        parser=lambda text: json.loads(text),
        repair_schema_prompt='[{"field":"current_goal","candidate_value":"..."}]',
        policy=StructuredJSONPolicy(
            name="growth_engine",
            response_max_tokens=220,
            repair_max_tokens=320,
        ),
    )

    repair_kwargs = router.generate.await_args_list[1].kwargs
    assert "[] または [{...}] のみ" in repair_kwargs["system_prompt"]
    assert "候補は最大1件" in repair_kwargs["system_prompt"]
    assert "切れた末尾オブジェクトは削ってよい" in repair_kwargs["user_prompt"]


@async_to_sync
async def test_generate_structured_json_repair_prompt_preserves_object_root_shape() -> None:
    router = MagicMock()
    router.generate = AsyncMock(
        side_effect=[
            _make_response('{"summary":"broken"', done_reason="length"),
            _make_response('{"summary":"fixed"}'),
        ]
    )

    await generate_structured_json(
        router=router,
        provider="ollama",
        model="test_model",
        system_prompt="system",
        user_prompt="user",
        parser=_parse_object,
        repair_schema_prompt='{"summary":"..."}',
        policy=StructuredJSONPolicy(
            name="scene_script_turn_1",
            response_max_tokens=220,
            repair_max_tokens=320,
        ),
    )

    repair_kwargs = router.generate.await_args_list[1].kwargs
    assert "返答は {...} の JSON object" in repair_kwargs["system_prompt"]
    assert "[] または [{...}]" not in repair_kwargs["system_prompt"]


@async_to_sync
async def test_generate_structured_json_retries_original_generation_when_primary_text_is_empty() -> None:
    router = MagicMock()
    router.generate = AsyncMock(
        side_effect=[
            _make_response("", done_reason="length"),
            _make_response('{"summary":"fixed"}'),
        ]
    )

    await generate_structured_json(
        router=router,
        provider="ollama",
        model="test_model",
        system_prompt="system",
        user_prompt="user",
        parser=_parse_object,
        repair_schema_prompt='{"summary":"..."}',
        policy=StructuredJSONPolicy(
            name="story_memory",
            response_max_tokens=220,
            repair_max_tokens=320,
        ),
    )

    retry_kwargs = router.generate.await_args_list[1].kwargs
    assert retry_kwargs["system_prompt"] == "system"
    assert "JSON" in retry_kwargs["user_prompt"]
    assert retry_kwargs["max_tokens"] < 320


@async_to_sync
async def test_generate_structured_json_retries_empty_length_response_with_reduced_budget() -> None:
    router = MagicMock()
    router.generate = AsyncMock(
        side_effect=[
            _make_response("", done_reason="length"),
            _make_response("", done_reason="length"),
            _make_response('{"summary":"fixed"}'),
        ]
    )

    result = await generate_structured_json(
        router=router,
        provider="ollama",
        model="test_model",
        system_prompt="system",
        user_prompt="user",
        parser=_parse_object,
        repair_schema_prompt='{"summary":"..."}',
        policy=StructuredJSONPolicy(
            name="story_memory",
            response_max_tokens=220,
            repair_max_tokens=320,
        ),
    )

    assert result.parsed == {"summary": "fixed"}
    assert result.used_repair is True
    assert router.generate.await_count == 3
    second_retry_kwargs = router.generate.await_args_list[2].kwargs
    assert second_retry_kwargs["max_tokens"] < router.generate.await_args_list[1].kwargs["max_tokens"]


@async_to_sync
async def test_generate_structured_json_logs_policy_name_on_empty_final(caplog) -> None:
    router = MagicMock()
    router.generate = AsyncMock(
        side_effect=[
            _make_response("", done_reason="length"),
            _make_response('{"summary":"fixed"}'),
        ]
    )

    with caplog.at_level(logging.WARNING):
        await generate_structured_json(
            router=router,
            provider="ollama",
            model="gemma4:e4b",
            system_prompt="system",
            user_prompt="user",
            parser=_parse_object,
            repair_schema_prompt='{"summary":"..."}',
            policy=StructuredJSONPolicy(
                name="novel_generator",
                response_max_tokens=220,
                repair_max_tokens=320,
            ),
        )

    assert any("structured_generation: empty final response during primary call" in message for message in caplog.messages)
    assert any("policy=novel_generator" in message for message in caplog.messages)
    assert any(getattr(record, "structured_policy_name", "") == "novel_generator" for record in caplog.records)


@async_to_sync
async def test_generate_structured_json_passes_request_tags_to_router_calls() -> None:
    router = MagicMock()
    router.generate = AsyncMock(
        side_effect=[
            _make_response("", done_reason="length"),
            _make_response('{"summary":"fixed"}'),
        ]
    )

    await generate_structured_json(
        router=router,
        provider="ollama",
        model="gemma4:e4b",
        system_prompt="system",
        user_prompt="user",
        parser=_parse_object,
        repair_schema_prompt='{"summary":"..."}',
        policy=StructuredJSONPolicy(
            name="story_memory",
            response_max_tokens=220,
            repair_max_tokens=320,
        ),
    )

    primary_kwargs = router.generate.await_args_list[0].kwargs
    retry_kwargs = router.generate.await_args_list[1].kwargs
    assert primary_kwargs["request_tag"] == "structured:story_memory:primary"
    assert retry_kwargs["request_tag"] == "structured:story_memory:empty_retry_1"

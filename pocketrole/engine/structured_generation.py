"""Shared helper for short structured JSON generation tasks."""

from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

from engine.config import ModelProfileConfig
from engine.llm.router import LLMRouter

T = TypeVar("T")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StructuredJSONPolicy:
    name: str
    response_max_tokens: int
    repair_max_tokens: int
    temperature: float = 0.2
    reasoning_mode: str = "off"
    repair_reasoning_mode: str = "off"


@dataclass(frozen=True)
class StructuredJSONResult:
    parsed: Any | None
    raw_text: str
    done_reason: str | None
    used_repair: bool
    parser_error: str | None = None


def _response_text(response: Any) -> str:
    final_text = getattr(response, "final_text", None)
    if isinstance(final_text, str):
        return final_text
    text = getattr(response, "text", "")
    return text if isinstance(text, str) else ""


def _resolve_profile(router: Any, provider: str, model: str) -> ModelProfileConfig | None:
    if isinstance(router, LLMRouter):
        return router.get_model_profile(provider, model)
    return None


def _cap_max_tokens(profile_value: int | None, requested: int) -> int:
    if profile_value is None:
        return requested
    return min(profile_value, requested)


def _reduced_retry_budget(max_tokens: int) -> int:
    return max(64, int(max_tokens * 0.6))


def _safe_parse(parser: Callable[[str], T | None], text: str) -> tuple[T | None, str | None]:
    try:
        return parser(text), None
    except (TypeError, ValueError, KeyError) as exc:
        return None, str(exc)


def _repair_root_contract(repair_schema_prompt: str) -> str:
    schema = str(repair_schema_prompt or "").lstrip()
    if schema.startswith("["):
        return "返答は [] または [{...}] のみで、候補は最大1件までにしてください。"
    if schema.startswith("{"):
        return "返答は {...} の JSON object のみで、schema と同じ root shape を保ってください。"
    return "返答は schema と同じ root shape の JSON のみを返してください。"


async def generate_structured_json(
    *,
    router: Any,
    provider: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    parser: Callable[[str], T | None],
    repair_schema_prompt: str,
    policy: StructuredJSONPolicy,
) -> StructuredJSONResult:
    """Generate structured JSON once, then try a single repair pass if needed."""
    profile = _resolve_profile(router, provider, model)
    primary = await router.generate(
        provider=provider,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        model=model,
        temperature=policy.temperature,
        max_tokens=_cap_max_tokens(
            profile.max_tokens if profile is not None else None,
            policy.response_max_tokens,
        ),
        reasoning_mode=policy.reasoning_mode,
        request_tag=f"structured:{policy.name}:primary",
    )
    primary_text = _response_text(primary)
    done_reason = getattr(primary, "done_reason", None)
    parsed, parser_error = _safe_parse(parser, primary_text)
    if parsed is not None and done_reason != "length":
        return StructuredJSONResult(
            parsed=parsed,
            raw_text=primary_text,
            done_reason=done_reason,
            used_repair=False,
            parser_error=parser_error,
        )
    if not primary_text.strip() and done_reason == "length":
        logger.warning(
            "structured_generation: empty final response during primary call policy=%s",
            policy.name,
            extra={
                "structured_policy_name": policy.name,
                "llm_provider": provider,
                "llm_model": model,
                "llm_system_prompt_chars": len(system_prompt),
                "llm_user_prompt_chars": len(user_prompt),
            },
        )

    if primary_text.strip():
        repair = await router.generate(
            provider=provider,
            system_prompt=(
                "あなたは壊れた応答を JSON に整形する修復器です。\n"
                "説明文や前置きは不要です。指定 schema に合う JSON のみを返してください。\n"
                f"{_repair_root_contract(repair_schema_prompt)}\n"
                "欠けた閉じ括弧や閉じ角括弧を補い、末尾カンマは除去して構いません。\n"
                f"schema:\n{repair_schema_prompt}"
            ),
            user_prompt=(
                "次の壊れた応答を schema に合わせて修復してください。\n"
                "修復できない場合は空の有効 JSON を返してください。\n"
                "閉じ括弧や閉じ角括弧が欠けていれば補い、説明文は削除してください。\n"
                "切れた末尾オブジェクトは削ってよいです。\n\n"
                f"{primary_text}"
            ),
            model=model,
            temperature=0.0,
            max_tokens=_cap_max_tokens(
                profile.recovery_max_tokens if profile is not None else None,
                policy.repair_max_tokens,
            ),
            reasoning_mode=policy.repair_reasoning_mode,
            request_tag=f"structured:{policy.name}:repair",
        )
    else:
        empty_retry_max_tokens = _reduced_retry_budget(
            _cap_max_tokens(
                profile.recovery_max_tokens if profile is not None else None,
                policy.repair_max_tokens,
            )
        )
        repair = await router.generate(
            provider=provider,
            system_prompt=system_prompt,
            user_prompt=(
                user_prompt
                + "\n\n"
                + "前回は空でした。説明文なしで schema に合う JSON のみを返してください。"
                + f"\nschema:\n{repair_schema_prompt}"
            ),
            model=model,
            temperature=0.0,
            max_tokens=empty_retry_max_tokens,
            reasoning_mode=policy.repair_reasoning_mode,
            request_tag=f"structured:{policy.name}:empty_retry_1",
        )
    repair_text = _response_text(repair)
    repair_done_reason = getattr(repair, "done_reason", None)
    if not repair_text.strip() and repair_done_reason == "length":
        logger.warning(
            "structured_generation: empty final response during retry policy=%s",
            policy.name,
            extra={
                "structured_policy_name": policy.name,
                "llm_provider": provider,
                "llm_model": model,
                "llm_system_prompt_chars": len(system_prompt),
                "llm_user_prompt_chars": len(user_prompt),
            },
        )
        reduced_budget = _reduced_retry_budget(
            _cap_max_tokens(
                profile.recovery_max_tokens if profile is not None else None,
                policy.repair_max_tokens,
            )
        )
        final_retry_budget = _reduced_retry_budget(reduced_budget)
        final_retry = await router.generate(
            provider=provider,
            system_prompt=system_prompt,
            user_prompt=(
                user_prompt
                + "\n\n"
                + "前回まで空でした。説明文なしで schema に合う JSON のみを、短く返してください。"
                + f"\nschema:\n{repair_schema_prompt}"
            ),
            model=model,
            temperature=0.0,
            max_tokens=final_retry_budget,
            reasoning_mode=policy.repair_reasoning_mode,
            request_tag=f"structured:{policy.name}:empty_retry_2",
        )
        repair_text = _response_text(final_retry)
    repaired, repair_parser_error = _safe_parse(parser, repair_text)
    return StructuredJSONResult(
        parsed=repaired,
        raw_text=primary_text,
        done_reason=done_reason,
        used_repair=True,
        parser_error=repair_parser_error if repair_parser_error is not None else parser_error,
    )

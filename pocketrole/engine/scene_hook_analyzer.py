"""engine/scene_hook_analyzer.py — closed scene hook consolidation."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from db.db_manager import DatabaseManager
from engine.llm.router import LLMRouter

logger = logging.getLogger(__name__)

_VALID_HOOK_TYPES = frozenset({"question", "promise", "conflict"})


class SceneHookAnalyzer:
    """closed conversation scene の unresolved hooks を統合する。"""

    def __init__(
        self,
        story_id: str,
        db: DatabaseManager,
        llm_router: LLMRouter,
        *,
        llm_provider: str = "",
        llm_model: str = "",
    ) -> None:
        self.story_id = story_id
        self._db = db
        self._llm = llm_router
        self._llm_provider = llm_provider
        self._llm_model = llm_model

    async def analyze_closed_scene(self, scene_id: int) -> dict[str, Any] | None:
        scene = await self._db.get_story_scene(scene_id)
        if scene is None or scene.get("scene_type") != "conversation":
            return None

        hooks = await self._db.get_open_story_hooks_for_scene(self.story_id, scene_id)
        if not hooks:
            return None

        compact_for_gemma = self._should_compact_for_gemma()
        logs = await self._db.get_scene_logs(scene_id, limit=4 if compact_for_gemma else 12)
        system_prompt = (
            "あなたは会話シーン終了時の hook 整理係です。"
            "未解決の約束・対立・問いを、解決済みか、残すべきか、統合すべきかだけ判断してください。"
            "回答は必ず JSON オブジェクトで返してください。"
        )
        log_lines = "\n".join(
            f"- log#{log.get('id')}: {log.get('char_id')}: "
            f"{self._compact_prompt_text(log.get('message'), 48 if compact_for_gemma else 180)}"
            for log in logs
        ) or "（ログなし）"
        hook_lines = "\n".join(
            f"- hook#{hook.get('id')}: [{hook.get('hook_type')}] "
            f"{self._compact_prompt_text(hook.get('description'), 48 if compact_for_gemma else 180)}"
            for hook in hooks
        )
        schema_example = (
            '{"refined_outcome_summary":"...",'
            '"resolve_hook_ids":[1],"keep_hook_ids":[2],'
            '"merged_hooks":[{"hook_type":"promise","description":"..."}]}'
            if compact_for_gemma
            else
            '{'
            '"refined_outcome_summary": "...", '
            '"resolve_hook_ids": [1], '
            '"keep_hook_ids": [2], '
            '"merged_hooks": [{"hook_type":"promise","title":"...","description":"...","priority":0.8}]'
            '}'
        )
        user_prompt = (
            f"scene_id: {scene_id}\n"
            f"close summary: {self._compact_prompt_text(scene.get('outcome_summary', ''), 60 if compact_for_gemma else 220)}\n\n"
            f"scene logs:\n{log_lines}\n\n"
            f"open hooks:\n{hook_lines}\n\n"
            "JSON 形式:\n"
            f"{schema_example}"
        )
        response = await self._llm.generate(
            provider=self._llm_provider,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=self._llm_model,
            request_tag=f"scene_hook_analyzer:scene_{scene_id}",
            **self._generation_kwargs(),
        )
        valid_hook_ids = {int(hook["id"]) for hook in hooks}
        return _parse_scene_hook_result(response.text, valid_hook_ids=valid_hook_ids)

    def _generation_kwargs(self) -> dict[str, int | float | str]:
        if not self._should_compact_for_gemma():
            return {}
        return {
            "max_tokens": 240,
            "temperature": 0.0,
            "reasoning_mode": "off",
        }

    def _should_compact_for_gemma(self) -> bool:
        return self._llm_provider == "ollama" and self._llm_model.lower().startswith("gemma4")

    @staticmethod
    def _compact_prompt_text(text: Any, max_chars: int) -> str:
        cleaned = re.sub(r"\s+", " ", str(text or "").strip())
        if len(cleaned) <= max_chars:
            return cleaned
        return cleaned[: max_chars - 1].rstrip() + "…"


def _parse_scene_hook_result(
    text: str,
    *,
    valid_hook_ids: set[int],
) -> dict[str, Any] | None:
    cleaned = re.sub(r"```(?:json)?\s*", "", text).replace("```", "").strip()
    data: Any = None
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                pass

    if not isinstance(data, dict):
        return None

    resolve_hook_ids = [
        int(hook_id)
        for hook_id in data.get("resolve_hook_ids", [])
        if _validated_hook_id(hook_id, valid_hook_ids) is not None
    ]
    keep_hook_ids = [
        int(hook_id)
        for hook_id in data.get("keep_hook_ids", [])
        if _validated_hook_id(hook_id, valid_hook_ids) is not None
    ]

    merged_hooks: list[dict[str, Any]] = []
    for item in data.get("merged_hooks", []):
        if not isinstance(item, dict):
            continue
        hook_type = str(item.get("hook_type") or "")
        description = str(item.get("description") or "").strip()
        if hook_type not in _VALID_HOOK_TYPES or not description:
            continue
        merged_hooks.append(
            {
                "hook_type": hook_type,
                "title": str(item.get("title") or "統合フック"),
                "description": description,
                "priority": max(0.0, min(1.0, float(item.get("priority", 0.7)))),
            }
        )

    refined_outcome_summary = str(data.get("refined_outcome_summary") or "").strip()
    return {
        "refined_outcome_summary": refined_outcome_summary,
        "resolve_hook_ids": resolve_hook_ids,
        "keep_hook_ids": keep_hook_ids,
        "merged_hooks": merged_hooks,
    }


def _validated_hook_id(raw: Any, valid_hook_ids: set[int]) -> int | None:
    try:
        hook_id = int(raw)
    except (TypeError, ValueError):
        return None
    if hook_id not in valid_hook_ids:
        return None
    return hook_id

"""
engine/character_evolution.py — キャラクター動的進化管理クラス

N ラウンドごとに各キャラの最近メモリを LLM に渡し、
フィールド変化を JSON で取得 → character_evolution テーブルへ保存する。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from db.db_manager import DatabaseManager
from engine.config import CharacterEvolutionConfig
from engine.llm.router import LLMRouter

logger = logging.getLogger(__name__)

# ============================================================
# 定数
# ============================================================

VALID_EVOLVABLE_FIELDS = frozenset({
    "current_goal",
    "current_worry",
    "personality_core",
})

_EVOLUTION_SYSTEM_PROMPT = """\
あなたはキャラクターの内面変化を観察する記録係です。
キャラの最近の経験（メモリ）をもとに、内面（目標・悩み・性格）に変化があれば提案してください。
変化がなければ空のリストを返してください。
回答は必ず JSON 配列形式で返してください。
"""


# ============================================================
# メインクラス
# ============================================================


class CharacterEvolutionManager:
    """キャラクター動的進化の管理クラス。"""

    def __init__(
        self,
        story_id: str,
        db: DatabaseManager,
        llm_router: LLMRouter,
        config: CharacterEvolutionConfig,
        llm_provider: str = "",
        llm_model: str = "",
    ) -> None:
        self.story_id = story_id
        self._db = db
        self._llm_router = llm_router
        self._config = config
        self._llm_provider = llm_provider
        self._llm_model = llm_model

    async def process_round(
        self, turn_number: int, characters: list[dict[str, Any]]
    ) -> None:
        """N ラウンドごとに各キャラの進化チェックを行う。"""
        if not self._config.enabled:
            return
        if turn_number % self._config.check_interval_rounds != 0:
            return
        for char in characters:
            await self._check_character(turn_number, char)

    async def get_evolution_overlay(self, char_id: str) -> dict[str, str]:
        """キャラの現在の evolution overlay を返す。"""
        return await self._db.get_latest_evolution_overlay(self.story_id, char_id)

    async def _check_character(
        self, turn_number: int, char: dict[str, Any]
    ) -> None:
        """単一キャラの進化チェック・保存を行う。"""
        # 1. 最近メモリ取得
        recent_memories = await self._db.get_relevant_story_memories(
            self.story_id, char["id"], limit=10
        )
        # 2. メモリが空ならスキップ（LLM 呼ばない）
        if not recent_memories:
            return
        # 3. 現在のオーバーレイ取得
        overlay = await self._db.get_latest_evolution_overlay(self.story_id, char["id"])
        # 4. プロンプト構築
        user_prompt = _build_prompt(char, overlay, recent_memories)
        # 5. LLM 呼び出し
        response = await self._llm_router.generate(
            provider=self._llm_provider,
            system_prompt=_EVOLUTION_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            model=self._llm_model,
            request_tag=f"character_evolution:{char['id']}:turn_{turn_number}",
        )
        # 6. パース（source_memory_id の検証用に有効 ID セットを構築）
        valid_memory_ids = {int(m["id"]) for m in recent_memories if m.get("id") is not None}
        changes = _parse_evolution_list(
            response.text, self._config.max_changes_per_check,
            valid_memory_ids=valid_memory_ids,
        )
        # 7. 保存
        for change in changes:
            change["char_id"] = char["id"]
            change["turn_number"] = turn_number
            await self._db.insert_evolution(self.story_id, change)
        if changes:
            logger.info(
                "CharacterEvolutionManager: saved %d changes (story_id=%s, char_id=%s, turn=%d)",
                len(changes),
                self.story_id,
                char["id"],
                turn_number,
            )


# ============================================================
# ヘルパー関数
# ============================================================


def _build_prompt(
    char: dict[str, Any],
    overlay: dict[str, str],
    memories: list[dict[str, Any]],
) -> str:
    """LLM へ送るユーザープロンプトを構築する。

    overlay の値でキャラの現在フィールドを上書きする。
    """
    current_goal = overlay.get("current_goal") or char.get("current_goal") or "（不明）"
    current_worry = overlay.get("current_worry") or char.get("current_worry") or "（不明）"
    personality_core = overlay.get("personality_core") or char.get("personality_core") or "（不明）"

    lines: list[str] = [
        f"【キャラクター: {char.get('name_ja', char['id'])}】",
        f"現在の目標: {current_goal}",
        f"現在の悩み: {current_worry}",
        f"性格の核: {personality_core}",
        "",
        "【最近の経験（メモリ）】",
    ]
    for mem in memories:
        summary = mem.get("summary") or ""
        lines.append(f"- {summary}")

    lines.extend([
        "",
        "上記の経験をもとに、キャラの内面変化を提案してください。",
        f"変化できるフィールドは {sorted(VALID_EVOLVABLE_FIELDS)} のいずれかです。",
        "変化がない場合は空のリストを返してください。",
        "",
        "回答は以下の JSON 配列形式で返してください:",
        '[{"field": "current_goal", "new_value": "新しい目標", '
        '"reason": "理由", "source_memory_id": null}]',
    ])
    return "\n".join(lines)


def _validated_memory_id(
    raw: Any, valid_ids: set[int] | None
) -> int | None:
    """source_memory_id を検証する。無効なら None を返す。"""
    if raw is None:
        return None
    try:
        mid = int(raw)
    except (TypeError, ValueError):
        return None
    if valid_ids is not None and mid not in valid_ids:
        return None
    return mid


def _parse_evolution_list(
    text: str, max_count: int,
    valid_memory_ids: set[int] | None = None,
) -> list[dict[str, Any]]:
    """LLM 応答テキストをパースして evolution の dict リストを返す。

    コードフェンス除去 → json.loads → 失敗時は正規表現再試行 → 失敗は [] 返す。
    各アイテムのバリデーションと max_count 制限を適用する。
    """
    # コードフェンス除去
    cleaned = re.sub(r"```json\s*", "", text)
    cleaned = re.sub(r"```\s*", "", cleaned).strip()

    parsed: list[Any] | None = None

    # 直接パース試行
    try:
        result = json.loads(cleaned)
        if isinstance(result, list):
            parsed = result
    except (json.JSONDecodeError, ValueError):
        pass

    # 失敗時: 正規表現で配列部分を抽出して再試行
    if parsed is None:
        match = re.search(r"\[.*\]", cleaned, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group())
                if isinstance(result, list):
                    parsed = result
            except (json.JSONDecodeError, ValueError):
                pass

    if parsed is None:
        logger.warning(
            "CharacterEvolutionManager._parse_evolution_list: JSON parse failed, text=%r",
            text[:100],
        )
        return []

    changes: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        field = item.get("field")
        if field not in VALID_EVOLVABLE_FIELDS:
            continue
        new_value = item.get("new_value")
        if not isinstance(new_value, str):
            continue
        changes.append({
            "field": field,
            "new_value": new_value,
            "reason": item.get("reason") or "経験による変化",
            "source_memory_id": _validated_memory_id(
                item.get("source_memory_id"), valid_memory_ids
            ),
            "previous_value": item.get("previous_value"),
        })
        if len(changes) >= max_count:
            break

    return changes

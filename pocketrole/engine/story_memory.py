"""
engine/story_memory.py — ストーリーメモリ管理クラス

チャットログを N ターンごとに LLM で要約し、story_memory テーブルへ保存する。
PromptBuilder から get_relevant_memories() で取得される。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from db.db_manager import DatabaseManager
from engine.config import StoryMemoryConfig
from engine.llm.router import LLMRouter
from engine.structured_generation import StructuredJSONPolicy, generate_structured_json

logger = logging.getLogger(__name__)

# ============================================================
# 定数
# ============================================================

_VALID_MEMORY_TYPES = frozenset({
    "scene_summary",
    "relationship_change",
    "secret_revealed",
    "conflict",
    "resolution",
    "character_growth",
    "world_change",
})

_VALID_EMOTIONAL_TONES = frozenset({
    "tense",
    "warm",
    "sad",
    "comic",
    "mysterious",
    "dramatic",
    "peaceful",
})

_SUMMARY_SYSTEM_PROMPT = """\
あなたは物語の記録係です。会話ログを読み、物語として何が起きたかを簡潔に要約してください。
キャラの具体的な台詞を引用せず、出来事と感情の動きに焦点を当ててください。
"""


# ============================================================
# メインクラス
# ============================================================

class StoryMemoryManager:
    """ストーリーメモリの生成・取得を管理する。"""

    def __init__(
        self,
        story_id: str,
        db: DatabaseManager,
        llm_router: LLMRouter,
        config: StoryMemoryConfig,
        llm_provider: str = "",
        llm_model: str = "",
    ) -> None:
        self.story_id = story_id
        self._db = db
        self._llm_router = llm_router
        self._config = config
        self._llm_provider = llm_provider
        self._llm_model = llm_model

    async def process_round(self, turn_number: int) -> None:
        """N ターンごとにチャットログを LLM で要約し story_memory に保存する。"""
        if not self._config.enabled:
            return
        if turn_number == 0 or turn_number % self._config.summarize_interval_turns != 0:
            return
        await self._summarize(turn_number)

    async def get_relevant_memories(self, char_id: str) -> list[dict[str, Any]]:
        """PromptBuilder 向けに関連記憶を返す。importance_threshold 以下は除外する。"""
        if not self._config.enabled:
            return []
        rows = await self._db.get_relevant_story_memories(
            self.story_id, char_id, limit=self._config.max_injection_count
        )
        threshold = self._config.importance_threshold
        return [r for r in rows if r["importance"] >= threshold]

    async def _summarize(self, turn_number: int) -> None:
        """チャットログを取得し、LLM で要約して story_memory に保存する。"""
        # 1. ストーリー情報取得
        story = await self._db.get_story(self.story_id)
        world_rules: str = (story or {}).get("world_rules") or "（設定なし）"

        # 2. キャラクター名マップ構築
        chars = await self._db.get_characters(self.story_id)
        char_name_map: dict[str, str] = {c["id"]: c["name_ja"] for c in chars}

        # 3. 対象ターンのチャットログを抽出
        since_turn = turn_number - self._config.summarize_interval_turns
        all_logs = await self._db.get_chat_logs(self.story_id)
        recent_logs = [
            log for log in all_logs
            if log["turn_number"] > since_turn and log["char_id"] != "_narrator"
        ]

        # 4. ログが 0 件ならスキップ
        if not recent_logs:
            logger.warning(
                "StoryMemoryManager._summarize: no logs in range (story_id=%s, turn=%d)",
                self.story_id,
                turn_number,
            )
            return

        # 5. コンテキスト用の既存メモリを取得
        context_memories = await self._db.get_story_memories_since_turn(
            self.story_id, since_turn=turn_number - 50, limit=5
        )

        # 6. プロンプト構築
        user_prompt = self._build_user_prompt(story, char_name_map, recent_logs, context_memories)

        # 7. LLM 呼び出し + repair
        provider = self._llm_provider
        model = self._llm_model
        result = await generate_structured_json(
            router=self._llm_router,
            provider=provider,
            model=model,
            system_prompt=_SUMMARY_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            parser=self._try_parse_json,
            repair_schema_prompt=(
                '{"summary":"...",'
                '"memory_type":"scene_summary",'
                '"involved_chars":["char_id"],'
                '"importance":0.5,'
                '"emotional_tone":"comic"}'
            ),
            policy=self._structured_json_policy(),
        )
        if not result.raw_text:
            logger.warning(
                "StoryMemoryManager._summarize: empty LLM response (story_id=%s, turn=%d)",
                self.story_id,
                turn_number,
            )
            return

        # 8. パース → 保存
        memory_dict = self._normalize_memory_payload(
            result.parsed,
            raw_text=result.raw_text,
            turn_number=turn_number,
        )
        await self._db.save_story_memory(self.story_id, memory_dict)
        logger.info(
            "StoryMemoryManager: saved memory (story_id=%s, turn=%d, type=%s)",
            self.story_id,
            turn_number,
            memory_dict["memory_type"],
        )

    def _build_user_prompt(
        self,
        story: dict[str, Any] | None,
        char_name_map: dict[str, str],
        recent_logs: list[dict[str, Any]],
        context_memories: list[dict[str, Any]],
    ) -> str:
        """LLM へ送るユーザープロンプトを構築する。"""
        compact_for_gemma = self._llm_provider == "ollama" and self._llm_model.lower().startswith("gemma4")
        world_rules: str = self._compact_prompt_text(
            (story or {}).get("world_rules") or "（設定なし）",
            120 if compact_for_gemma else 600,
        )

        lines: list[str] = ["【世界設定】", world_rules, "", "【会話ログ】"]
        log_limit = 5 if compact_for_gemma else 12
        log_char_limit = 76 if compact_for_gemma else 240
        for log in recent_logs[-log_limit:]:
            char_name = char_name_map.get(log["char_id"], log["char_id"])
            place_id = log.get("place_id") or ""
            lines.append(
                self._compact_prompt_text(
                    f"[{log['sim_datetime']}] {char_name}({place_id}): {log['message']}",
                    log_char_limit,
                )
            )

        if context_memories:
            lines.append("")
            lines.append("【既に記録されている物語の記憶】")
            memory_limit = 2 if compact_for_gemma else 3
            memory_char_limit = 56 if compact_for_gemma else 220
            for mem in context_memories[-memory_limit:]:
                lines.append(self._compact_prompt_text(mem["summary"], memory_char_limit))

        lines.extend([
            "",
            "回答は以下のJSON形式で返してください:",
            "{",
            '  "summary": "120字以内",',
            '  "memory_type": "scene_summary | ...",',
            '  "involved_chars": ["char_id1", ...],',
            '  "importance": 0.0〜1.0,',
            '  "emotional_tone": "tense | warm | ..."',
            "}",
        ])

        return "\n".join(lines)

    def _structured_json_policy(self) -> StructuredJSONPolicy:
        response_max_tokens = self._config.response_max_tokens
        repair_max_tokens = self._config.repair_max_tokens
        if self._llm_provider == "ollama" and self._llm_model.lower().startswith("gemma4"):
            response_max_tokens = min(response_max_tokens, 260)
            repair_max_tokens = min(repair_max_tokens, 360)
        return StructuredJSONPolicy(
            name="story_memory",
            response_max_tokens=response_max_tokens,
            repair_max_tokens=repair_max_tokens,
        )

    @staticmethod
    def _compact_prompt_text(text: str, max_chars: int) -> str:
        cleaned = re.sub(r"\s+", " ", str(text or "").strip())
        if len(cleaned) <= max_chars:
            return cleaned
        return cleaned[: max_chars - 1].rstrip() + "…"

    def _parse_response(self, text: str, turn_number: int) -> dict[str, Any]:
        """LLM 応答テキストをパースして story_memory 用 dict を返す。"""
        parsed = self._try_parse_json(text)
        return self._normalize_memory_payload(parsed, raw_text=text, turn_number=turn_number)

    def _normalize_memory_payload(
        self,
        parsed: dict[str, Any] | None,
        *,
        raw_text: str,
        turn_number: int,
    ) -> dict[str, Any]:
        if parsed is None:
            logger.warning(
                "StoryMemoryManager._parse_response: JSON parse failed, using defaults (turn=%d)",
                turn_number,
            )
            return {
                "memory_type": "scene_summary",
                "importance": 0.5,
                "summary": raw_text[:200],
                "involved_chars": [],
                "emotional_tone": None,
                "trigger_turn": turn_number,
            }

        # memory_type 正規化
        memory_type = parsed.get("memory_type", "scene_summary")
        if memory_type not in _VALID_MEMORY_TYPES:
            memory_type = "scene_summary"

        # emotional_tone 正規化
        emotional_tone = parsed.get("emotional_tone")
        if emotional_tone not in _VALID_EMOTIONAL_TONES:
            emotional_tone = None

        # importance クランプ
        try:
            importance = max(0.0, min(1.0, float(parsed.get("importance", 0.5))))
        except (TypeError, ValueError):
            importance = 0.5

        # summary 切り詰め
        summary = str(parsed.get("summary", ""))[:500]

        return {
            "memory_type": memory_type,
            "importance": importance,
            "summary": summary,
            "involved_chars": parsed.get("involved_chars", []),
            "emotional_tone": emotional_tone,
            "trigger_turn": turn_number,
        }

    @staticmethod
    def _try_parse_json(text: str) -> dict[str, Any] | None:
        """JSON を直接パース、失敗したらコードフェンスを除去して再試行する。"""
        try:
            result = json.loads(text)
            if isinstance(result, dict):
                return result
        except (json.JSONDecodeError, ValueError):
            pass

        # ```json ... ``` フェンスを除去して再試行
        stripped = re.sub(r"```json\s*", "", text)
        stripped = re.sub(r"```\s*", "", stripped).strip()
        try:
            result = json.loads(stripped)
            if isinstance(result, dict):
                return result
        except (json.JSONDecodeError, ValueError):
            pass

        return None

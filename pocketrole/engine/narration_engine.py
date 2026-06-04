"""
engine/narration_engine.py — ラウンド完了後にナレーションを生成・保存する。

第三者視点のナレーションテキストを LLM で生成し、chat_logs に
char_id="_narrator" で保存する。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from engine.config import NarrationConfig
from engine.llm.base import LLMResponse
from engine.story_style import get_story_style_profile

if TYPE_CHECKING:
    from db.db_manager import DatabaseManager
    from engine.llm.router import LLMRouter

logger = logging.getLogger(__name__)

# msg_type → ナレーション指示文
_NARRATION_INSTRUCTIONS: dict[str, str] = {
    "narration_scene":      "情景・状況をナレーションしてください。",
    "narration_inner":      "登場人物の内面・心理状態をナレーションしてください。",
    "narration_transition": "場面転換を短く描写してください。",
    "narration_chapter":    "この場面に章タイトルをつけてください。",
    "narration_foreshadow": "不穏な予感や伏線をナレーションしてください。",
}

_COMPLETE_SENTENCE_PATTERN = re.compile(r'[。！？!?](?:[」』”"）》】]*)')


# ============================================================
# NarrationContext — process_round() に渡す状況コンテキスト
# ============================================================

@dataclass
class NarrationContext:
    """NarrationEngine.process_round() に渡す状況コンテキスト。"""

    sim_datetime: str                                          # "YYYY-MM-DDTHH:MM"
    place_id: str
    place_name: str
    world_rules: str                                          # stories.world_rules
    recent_log_lines: list[str]                               # ["キャラ名: 発言", ...]
    emotion_delta: float = 0.0                                # 今ラウンドの最大感情変動
    character_moved: bool = False                             # 今ラウンドに移動が発生したか
    story_memory_texts: list[str] = field(default_factory=list)  # StoryMemoryManager 由来


# ============================================================
# NarrationEngine
# ============================================================

class NarrationEngine:
    """ラウンド完了後にナレーションを生成し、chat_logs へ保存する。"""

    def __init__(
        self,
        story_id: str,
        db: DatabaseManager,
        llm_router: LLMRouter,
        config: NarrationConfig,
        llm_provider: str = "",
        llm_model: str = "",
    ) -> None:
        self._story_id = story_id
        self._db = db
        self._llm_router = llm_router
        self._config = config
        self._llm_provider = llm_provider
        self._llm_model = llm_model
        self._style_profile = get_story_style_profile(story_id)
        self._last_narration_turn: int = 0
        self._arcs_seen_at_last_chapter_break: int = 0

    # ------------------------------------------------------------------
    # 公開メソッド
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """DB から既存の closed arc 数を取得し、再起動時の chapter_break 連鎖を防ぐ。"""
        arcs = await self._db.get_arcs(self._story_id)
        self._arcs_seen_at_last_chapter_break = sum(
            1 for arc in arcs if arc["turn_to"] is not None
        )
        logger.info(
            "NarrationEngine initialized",
            extra={
                "story_id": self._story_id,
                "arcs_seen_at_last_chapter_break": self._arcs_seen_at_last_chapter_break,
            },
        )

    async def process_round(self, turn_number: int, ctx: NarrationContext) -> bool:
        """ラウンド完了時に呼び出す。章区切りが発生した場合 True を返す。"""
        if not self._config.enabled:
            return False

        # 章区切り判定（story_arc との連携）
        chapter_break = False
        if self._config.include_chapter_breaks:
            chapter_break = await self._check_chapter_break(turn_number)
            if chapter_break:
                text = await self._generate("narration_chapter", ctx)
                if text:
                    await self._save(text, "narration_chapter", ctx, turn_number)
                    logger.info(
                        "Chapter break narration generated",
                        extra={"story_id": self._story_id, "turn_number": turn_number},
                    )

        # 通常ナレーション生成（既存ロジック）
        should_gen, msg_type = self._should_generate(turn_number, ctx)
        if should_gen:
            text = await self._generate(msg_type, ctx)
            if text:
                await self._save(text, msg_type, ctx, turn_number)
                self._last_narration_turn = turn_number
                logger.info(
                    "Narration generated",
                    extra={
                        "story_id": self._story_id,
                        "turn_number": turn_number,
                        "msg_type": msg_type,
                        "place_id": ctx.place_id,
                    },
                )

        return chapter_break

    # ------------------------------------------------------------------
    # 内部メソッド
    # ------------------------------------------------------------------

    def _should_generate(
        self, turn_number: int, ctx: NarrationContext
    ) -> tuple[bool, str]:
        """トリガー判定（優先順位順）。

        Returns:
            (生成すべきか, msg_type)
        """
        # 優先度 1: 感情変動が閾値以上
        emotion_threshold = self._config.emotion_change_threshold
        if self._style_profile.narration_emotion_threshold is not None:
            emotion_threshold = max(emotion_threshold, self._style_profile.narration_emotion_threshold)
        if ctx.emotion_delta >= emotion_threshold:
            return True, "narration_inner"

        # 優先度 2: 移動が発生した
        if ctx.character_moved:
            if self._style_profile.narration_mode != "minimal_comic" or len(ctx.recent_log_lines) >= 2:
                return True, "narration_scene"

        # 優先度 3: インターバル到達
        interval_turns = self._config.min_interval_turns
        if self._style_profile.narration_min_interval_turns is not None:
            interval_turns = max(interval_turns, self._style_profile.narration_min_interval_turns)
        if (turn_number - self._last_narration_turn) >= interval_turns:
            return True, "narration_scene"

        return False, ""

    async def _check_chapter_break(self, turn_number: int) -> bool:
        """直近で閉じた story_arc が増えていれば章区切りと判定する。

        NovelGenerator が前ラウンドにアークを生成・完了した場合に True を返し、
        以降は同じアーク群では再発火しない（_arcs_seen_at_last_chapter_break で管理）。
        """
        arcs = await self._db.get_arcs(self._story_id)
        closed_count = sum(1 for arc in arcs if arc["turn_to"] is not None)
        if closed_count > self._arcs_seen_at_last_chapter_break:
            self._arcs_seen_at_last_chapter_break = closed_count
            return True
        return False

    def _build_system_prompt(self, ctx: NarrationContext) -> str:
        """ナレーター用システムプロンプトを構築する。"""
        compact_for_gemma = self._should_compact_for_gemma()
        world_rules = self._compact_prompt_text(
            ctx.world_rules,
            120 if compact_for_gemma else 600,
        )
        if not self._style_profile.allow_literary_narration:
            rules = "\n".join(f"- {rule}" for rule in self._style_profile.narration_rules)
            return (
                "あなたは会話劇を補助するナレーターです。\n"
                "第三者の短い状況メモとして日本語で書いてください。\n"
                "最後の文まで完結させ、文の途中では終えないでください。\n"
                f"{rules}\n"
                "\n"
                "【世界設定】\n"
                f"{world_rules}"
            )
        if compact_for_gemma:
            return (
                "あなたは小説のナレーターです。\n"
                "第三者視点で、場所と変化だけを1〜2文で具体的に書いてください。\n"
                "比喩・抽象論・長い前置きは禁止です。\n"
                "最後の文まで完結させ、文の途中では終えないでください。\n"
                "\n"
                "【世界設定】\n"
                f"{world_rules}"
            )
        return (
            "あなたは小説のナレーターです。\n"
            "物語を第三者視点で、文学的な日本語で描写してください。\n"
            "2〜4文、概ね400〜700字を目安に、情緒的だが要点を絞って書いてください。\n"
            "最後の文まで完結させ、文の途中では終えないでください。\n"
            "\n"
            "【世界設定】\n"
            f"{world_rules}"
        )

    def _build_user_prompt(self, msg_type: str, ctx: NarrationContext) -> str:
        """ナレーション生成用ユーザープロンプトを構築する。"""
        compact_for_gemma = self._should_compact_for_gemma()
        lines: list[str] = []

        lines.append("【現在の状況】")
        lines.append(f"日時: {ctx.sim_datetime}")
        lines.append(f"場所: {ctx.place_name}")

        if ctx.recent_log_lines:
            lines.append("")
            lines.append("【直前の出来事】")
            log_limit = 3 if compact_for_gemma else 8
            log_char_limit = 72 if compact_for_gemma else 220
            lines.extend(
                self._compact_prompt_text(line, log_char_limit)
                for line in self._compact_repeated_recent_tail_lines(
                    ctx.recent_log_lines[-log_limit:]
                )
            )

        if ctx.story_memory_texts:
            lines.append("")
            lines.append("【最近の重要な出来事】")
            memory_limit = 1 if compact_for_gemma else 3
            memory_char_limit = 56 if compact_for_gemma else 180
            for memory in ctx.story_memory_texts[:memory_limit]:
                lines.append(f"- {self._compact_prompt_text(memory, memory_char_limit)}")

        instruction = _NARRATION_INSTRUCTIONS.get(msg_type, "ナレーションしてください。")
        if compact_for_gemma and msg_type == "narration_scene":
            instruction = (
                "場所の変化か空気の切り替わりだけを、1〜2文で具体的に書いてください。"
            )
        elif self._style_profile.narration_mode == "minimal_comic" and msg_type != "narration_chapter":
            instruction = (
                "会話劇を邪魔しないように、状況を短く具体的に書いてください。"
            )
        lines.append("")
        lines.append(instruction)

        return "\n".join(lines)

    @staticmethod
    def _compact_repeated_recent_tail_lines(lines: list[str]) -> list[str]:
        """同一の締め句が連鎖した round を narration prompt で増幅させない。"""
        compacted: list[str] = []
        seen_tails: set[str] = set()
        for line in lines:
            text = str(line).strip()
            parts = [part.strip() for part in text.split("。") if part.strip()]
            if len(parts) < 2:
                compacted.append(text)
                continue
            tail = parts[-1]
            if tail in seen_tails:
                compacted.append("。".join(parts[:-1]))
                continue
            seen_tails.add(tail)
            compacted.append(text)
        return compacted

    async def _generate(self, msg_type: str, ctx: NarrationContext) -> str:
        """LLM を呼び出してナレーションテキストを生成する。"""
        provider = self._llm_provider
        model = self._llm_model
        system_prompt = self._build_system_prompt(ctx)
        user_prompt = self._build_user_prompt(msg_type, ctx)

        response: LLMResponse = await self._llm_router.generate(
            provider=provider,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            request_tag=f"narration:{msg_type}:{ctx.place_id or 'none'}",
            **self._generation_kwargs(msg_type),
        )
        response_text = response.text.strip()
        if response.done_reason != "length":
            return response_text

        logger.warning(
            "Narration response hit token limit; retrying with compact completion guidance",
            extra={
                "story_id": self._story_id,
                "msg_type": msg_type,
                "place_id": ctx.place_id,
                "llm_provider": provider,
                "llm_model": model,
                "response_chars": len(response_text),
            },
        )

        retry_response: LLMResponse = await self._llm_router.generate(
            provider=provider,
            system_prompt=system_prompt,
            user_prompt=self._build_retry_user_prompt(msg_type, ctx),
            model=model,
            request_tag=f"narration:{msg_type}:{ctx.place_id or 'none'}:retry",
            **self._retry_generation_kwargs(msg_type),
        )
        retry_text = retry_response.text.strip()
        if retry_text and retry_response.done_reason != "length":
            logger.info(
                "Narration recovered after token-limit retry",
                extra={
                    "story_id": self._story_id,
                    "msg_type": msg_type,
                    "place_id": ctx.place_id,
                    "llm_provider": provider,
                    "llm_model": model,
                    "response_chars": len(retry_text),
                },
            )
            return retry_text

        fallback_text = self._trim_to_last_complete_sentence(retry_text or response_text)
        if fallback_text:
            logger.warning(
                "Narration retry still incomplete; preserving only complete sentences",
                extra={
                    "story_id": self._story_id,
                    "msg_type": msg_type,
                    "place_id": ctx.place_id,
                    "llm_provider": provider,
                    "llm_model": model,
                    "response_chars": len(retry_text or response_text),
                    "fallback_chars": len(fallback_text),
                },
            )
            return fallback_text

        logger.warning(
            "Narration discarded because retry did not produce any complete sentence",
            extra={
                "story_id": self._story_id,
                "msg_type": msg_type,
                "place_id": ctx.place_id,
                "llm_provider": provider,
                "llm_model": model,
                "response_chars": len(retry_text or response_text),
            },
        )
        return ""

    def _build_retry_user_prompt(self, msg_type: str, ctx: NarrationContext) -> str:
        """length 終了時の再生成プロンプトを返す。"""
        base_prompt = self._build_user_prompt(msg_type, ctx)
        if msg_type == "narration_chapter":
            return (
                base_prompt
                + "\n\n"
                + "前回の章タイトルは文の途中で終わりました。"
                + "1行の章タイトルだけを、最後まで完結させて書き直してください。"
            )
        return (
            base_prompt
            + "\n\n"
            + "前回のナレーションは文の途中で終わりました。"
            + "同じ場面を保ちながら、要点を絞って2〜4文で書き直してください。"
            + "最後の文まで完結させ、文の途中では終えないでください。"
        )

    def _generation_kwargs(self, msg_type: str) -> dict[str, int | float | str]:
        """Gemma 向けに narration 生成 budget を締める。"""
        if not self._should_compact_for_gemma():
            if msg_type == "narration_chapter":
                return {"max_tokens": self._config.chapter_max_tokens}
            return {"max_tokens": self._config.response_max_tokens}
        if msg_type == "narration_chapter":
            return {"max_tokens": 80, "temperature": 0.0, "reasoning_mode": "off"}
        return {"max_tokens": 180, "temperature": 0.1, "reasoning_mode": "off"}

    def _retry_generation_kwargs(self, msg_type: str) -> dict[str, int | float | str]:
        """length 終了後の再生成 budget を返す。"""
        if self._should_compact_for_gemma():
            return self._generation_kwargs(msg_type)
        if msg_type == "narration_chapter":
            return {"max_tokens": self._config.chapter_max_tokens}
        return {"max_tokens": self._config.retry_max_tokens}

    def _should_compact_for_gemma(self) -> bool:
        return self._llm_provider == "ollama" and self._llm_model.lower().startswith("gemma4")

    @staticmethod
    def _compact_prompt_text(text: str, max_chars: int) -> str:
        cleaned = re.sub(r"\s+", " ", str(text or "").strip())
        if len(cleaned) <= max_chars:
            return cleaned
        return cleaned[: max_chars - 1].rstrip() + "…"

    @staticmethod
    def _trim_to_last_complete_sentence(text: str) -> str:
        """文末記号まで到達している部分だけを返す。"""
        cleaned = str(text or "").strip()
        last_match = None
        for match in _COMPLETE_SENTENCE_PATTERN.finditer(cleaned):
            last_match = match
        if last_match is None:
            return ""
        return cleaned[: last_match.end()].strip()

    async def _save(
        self,
        text: str,
        msg_type: str,
        ctx: NarrationContext,
        turn_number: int,
    ) -> None:
        """生成したナレーションを chat_logs に保存する。"""
        provider = self._llm_provider
        model = self._llm_model

        await self._db.insert_chat_log(
            self._story_id,
            {
                "sim_datetime": ctx.sim_datetime,
                "turn_number": turn_number,
                "char_id": "_narrator",
                "msg_type": msg_type,
                "place_id": ctx.place_id,
                "expression": "neutral",
                "message": text,
                "emotion_snapshot": None,
                "llm_provider": provider,
                "llm_model": model,
                "posted_to_web": 0,
            },
        )

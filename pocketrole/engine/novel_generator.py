"""engine/novel_generator.py — 小説自動生成エンジン。

会話ログを LLM で散文テキストに変換し、
story_arc / novel_output テーブルに保存する。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from engine.structured_generation import StructuredJSONPolicy, generate_structured_json

if TYPE_CHECKING:
    from db.db_manager import DatabaseManager
    from engine.config import NovelGeneratorConfig
    from engine.llm.router import LLMRouter

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# コンテキスト
# ------------------------------------------------------------------

@dataclass
class NovelGeneratorContext:
    """process_round に渡すコンテキスト情報。"""

    world_rules: str
    recent_log_lines: list[str]                              # ["キャラ名: 発言", ...]
    story_memory_texts: list[str] = field(default_factory=list)
    trigger: str = "session_end"                             # "session_end" | "chapter_break" | "scene_close" | "episode_close"
    source_log_ids: list[int] = field(default_factory=list)  # chat_log の id リスト
    scene_id: int | None = None
    scene_type: str | None = None
    scene_outcome_summary: str | None = None
    hook_summaries: list[str] = field(default_factory=list)
    tension_summaries: list[str] = field(default_factory=list)
    place_id: str | None = None
    participant_names: list[str] = field(default_factory=list)
    episode_id: int | None = None
    episode_type: str | None = None
    episode_goal: str | None = None
    episode_summary: str | None = None


# ------------------------------------------------------------------
# NovelGenerator
# ------------------------------------------------------------------

class NovelGenerator:
    """小説自動生成エンジン。

    会話ログを LLM で散文テキストに変換し、
    story_arc / novel_output テーブルに保存する。
    """

    def __init__(
        self,
        story_id: str,
        config: NovelGeneratorConfig,
        db: DatabaseManager,
        llm_router: LLMRouter,
        llm_provider: str = "",
        llm_model: str = "",
    ) -> None:
        self._story_id = story_id
        self._config = config
        self._db = db
        self._llm = llm_router
        self._llm_provider = llm_provider
        self._llm_model = llm_model

    # ------------------------------------------------------------------
    # 公開 API
    # ------------------------------------------------------------------

    async def process_round(
        self,
        turn_number: int,
        ctx: NovelGeneratorContext,
    ) -> None:
        """トリガー判定 → LLM 生成 → アーク作成 → novel_output 保存 → アーク完了。"""
        if not self._config.enabled:
            return

        if ctx.trigger == "session_end" and not self._config.generate_on_session_end:
            return
        if ctx.trigger == "chapter_break" and not self._config.generate_on_chapter_break:
            return
        existing_scene_arc: dict[str, Any] | None = None
        if ctx.trigger == "scene_close" and ctx.scene_id is not None:
            existing_scene_arc = await self._db.get_arc_by_source_scene_id(self._story_id, ctx.scene_id)
            if existing_scene_arc is not None:
                existing_outputs = await self._db.get_novel_outputs(
                    self._story_id,
                    int(existing_scene_arc["id"]),
                )
                if existing_outputs:
                    return

        if ctx.trigger == "scene_close":
            self._ensure_scene_close_context(ctx, existing_scene_arc=existing_scene_arc)

        if not ctx.recent_log_lines and ctx.trigger != "scene_close":
            return

        await self._generate_prose(turn_number, ctx, existing_scene_arc=existing_scene_arc)

    # ------------------------------------------------------------------
    # 内部メソッド
    # ------------------------------------------------------------------

    async def _generate_prose(
        self,
        turn_number: int,
        ctx: NovelGeneratorContext,
        *,
        existing_scene_arc: dict[str, Any] | None = None,
    ) -> None:
        """LLM でプロンプトを構築し、散文テキストを生成・保存する。"""
        system_prompt, user_prompt = self._build_generation_prompts(ctx)

        result = await generate_structured_json(
            router=self._llm,
            provider=self._llm_provider,
            model=self._llm_model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            parser=self._parse_prose_response,
            repair_schema_prompt='{"title":"...","summary":"...","prose":"..."}',
            policy=self._structured_json_policy(),
        )
        parsed = result.parsed
        if parsed is None:
            if ctx.trigger == "scene_close":
                parsed = self._build_scene_close_fallback(ctx, existing_scene_arc=existing_scene_arc)
            elif ctx.trigger == "episode_close":
                parsed = self._build_episode_close_fallback(ctx)
            else:
                logger.warning(
                    "novel_generator: LLM 応答のパースに失敗しました: story_id=%s, turn=%d",
                    self._story_id,
                    turn_number,
                )
                return
        if parsed is None:
            logger.warning(
                "novel_generator: LLM 応答のパースに失敗しました: story_id=%s, turn=%d",
                self._story_id,
                turn_number,
            )
            return

        if existing_scene_arc is None:
            arc_id = await self._db.insert_arc(
                self._story_id,
                {
                    "arc_type": "episode" if ctx.trigger == "episode_close" else "scene",
                    "title": parsed["title"],
                    "summary": parsed["summary"],
                    "turn_from": turn_number,
                    "source_scene_id": ctx.scene_id,
                },
            )
        else:
            arc_id = int(existing_scene_arc["id"])

        existing = await self._db.get_novel_outputs(self._story_id, arc_id)
        if existing:
            return
        ordering = len(existing) + 1

        await self._db.insert_novel_output(
            self._story_id,
            arc_id,
            {
                "content_type": "prose",
                "content": parsed["prose"],
                "source_log_ids": ctx.source_log_ids,
                "ordering": ordering,
            },
        )

        await self._db.close_arc(arc_id, turn_to=turn_number)

        logger.info(
            "novel_generator: 散文生成完了: story_id=%s, arc_id=%d, turn=%d",
            self._story_id,
            arc_id,
            turn_number,
        )

    def _build_generation_prompts(self, ctx: NovelGeneratorContext) -> tuple[str, str]:
        compact_for_gemma = self._llm_provider == "ollama" and self._llm_model.lower().startswith("gemma4")
        world_rules = self._compact_prompt_text(ctx.world_rules, 96 if compact_for_gemma else 600)

        memory_limit = 1 if compact_for_gemma else 3
        memory_char_limit = 48 if compact_for_gemma else 140
        memory_section = ""
        if ctx.story_memory_texts:
            lines = "\n".join(
                f"- {self._compact_prompt_text(text, memory_char_limit)}"
                for text in ctx.story_memory_texts[:memory_limit]
            )
            memory_section = f"\n【ストーリーメモリ】\n{lines}\n"

        scene_section = ""
        if ctx.trigger == "scene_close":
            scene_lines: list[str] = []
            summary_char_limit = 56 if compact_for_gemma else 160
            detail_char_limit = 36 if compact_for_gemma else 120
            hook_limit = 1 if compact_for_gemma else 3
            if compact_for_gemma and (ctx.scene_type or ctx.place_id):
                scene_meta = " / ".join(part for part in (ctx.scene_type, ctx.place_id) if part)
                scene_lines.append(f"- scene: {scene_meta}")
            else:
                if ctx.scene_type:
                    scene_lines.append(f"- scene_type: {ctx.scene_type}")
                if ctx.place_id:
                    scene_lines.append(f"- place_id: {ctx.place_id}")
            if ctx.participant_names:
                participant_names = ctx.participant_names[: (2 if compact_for_gemma else 6)]
                participant_text = ", ".join(participant_names)
                if compact_for_gemma and len(ctx.participant_names) > len(participant_names):
                    participant_text += ", ..."
                scene_lines.append(f"- participants: {participant_text}")
            if ctx.scene_outcome_summary:
                scene_lines.append(
                    f"- outcome: {self._compact_prompt_text(ctx.scene_outcome_summary, summary_char_limit)}"
                )
            if ctx.hook_summaries:
                scene_lines.append("- unresolved_hooks:")
                scene_lines.extend(
                    f"  - {self._compact_prompt_text(hook, detail_char_limit)}"
                    for hook in ctx.hook_summaries[:hook_limit]
                )
            if ctx.tension_summaries:
                scene_lines.append("- active_tensions:")
                scene_lines.extend(
                    f"  - {self._compact_prompt_text(tension, detail_char_limit)}"
                    for tension in ctx.tension_summaries[:hook_limit]
                )
            if scene_lines:
                scene_section = f"\n【閉じた場面の結果】\n" + "\n".join(scene_lines) + "\n"

        episode_section = ""
        if ctx.trigger == "episode_close":
            episode_lines: list[str] = []
            detail_char_limit = 48 if compact_for_gemma else 140
            if ctx.episode_type:
                episode_lines.append(f"- episode_type: {ctx.episode_type}")
            if ctx.episode_goal:
                episode_lines.append(f"- goal: {self._compact_prompt_text(ctx.episode_goal, detail_char_limit)}")
            if ctx.episode_summary:
                episode_lines.append(
                    f"- summary: {self._compact_prompt_text(ctx.episode_summary, detail_char_limit)}"
                )
            if episode_lines:
                episode_section = f"\n【閉じたエピソード】\n" + "\n".join(episode_lines) + "\n"

        system_prompt = (
            "あなたはプロのライトノベル作家です。\n"
            "次の世界設定と会話ログをもとに、小説形式の文章を生成してください。\n\n"
            f"【世界設定】\n{world_rules}\n"
            f"{memory_section}\n"
            f"{scene_section}\n"
            f"{episode_section}\n"
            '以下のJSON形式のみで回答してください:\n'
            '{"title": "場面タイトル（20字以内）", "summary": "場面の要約（50字以内）",'
            ' "prose": "小説テキスト（120〜220字）"}'
        )

        log_limit = 4 if compact_for_gemma else 8
        log_char_limit = 72 if compact_for_gemma else 220
        dialogue_lines = "\n".join(
            self._compact_prompt_text(line, log_char_limit)
            for line in ctx.recent_log_lines[-log_limit:]
        )
        user_prompt = f"【会話ログ】\n{dialogue_lines}"
        return system_prompt, user_prompt

    @staticmethod
    def _compact_prompt_text(text: str, max_chars: int) -> str:
        cleaned = re.sub(r"\s+", " ", str(text or "").strip())
        if len(cleaned) <= max_chars:
            return cleaned
        return cleaned[: max_chars - 1].rstrip() + "…"

    def _structured_json_policy(self) -> StructuredJSONPolicy:
        response_max_tokens = self._config.response_max_tokens
        repair_max_tokens = self._config.repair_max_tokens
        if self._llm_provider == "ollama" and self._llm_model.lower().startswith("gemma4"):
            response_max_tokens = min(response_max_tokens, 420)
            repair_max_tokens = min(repair_max_tokens, 520)
        return StructuredJSONPolicy(
            name="novel_generator",
            response_max_tokens=response_max_tokens,
            repair_max_tokens=repair_max_tokens,
        )

    @staticmethod
    def _ensure_scene_close_context(
        ctx: NovelGeneratorContext,
        *,
        existing_scene_arc: dict[str, Any] | None = None,
    ) -> None:
        if (
            existing_scene_arc is not None
            and (ctx.scene_outcome_summary is None or not str(ctx.scene_outcome_summary).strip())
        ):
            arc_summary = str(existing_scene_arc.get("summary") or "").strip()
            if arc_summary:
                ctx.scene_outcome_summary = arc_summary
        if ctx.scene_outcome_summary is None or not str(ctx.scene_outcome_summary).strip():
            ctx.scene_outcome_summary = NovelGenerator._fallback_scene_close_summary(ctx)
        if not ctx.recent_log_lines:
            seed_lines: list[str] = []
            if ctx.scene_outcome_summary:
                seed_lines.append(f"Scene: {ctx.scene_outcome_summary}")
            if existing_scene_arc is not None:
                arc_title = str(existing_scene_arc.get("title") or "").strip()
                if arc_title:
                    seed_lines.append(f"Arc: {arc_title}")
            if ctx.hook_summaries:
                seed_lines.extend(f"Hook: {hook}" for hook in ctx.hook_summaries[:2])
            if ctx.tension_summaries:
                seed_lines.extend(f"Tension: {tension}" for tension in ctx.tension_summaries[:2])
            if seed_lines:
                ctx.recent_log_lines = seed_lines

    @staticmethod
    def _fallback_scene_close_summary(ctx: NovelGeneratorContext) -> str:
        if ctx.scene_outcome_summary and str(ctx.scene_outcome_summary).strip():
            return str(ctx.scene_outcome_summary).strip()
        recent_lines = [
            line.split(": ", 1)[-1].strip()
            for line in ctx.recent_log_lines[-2:]
            if str(line).strip()
        ]
        recent_lines = [line for line in recent_lines if line]
        if recent_lines:
            excerpt = " ".join(recent_lines)[:100]
            if ctx.place_id:
                return f"{ctx.place_id}では「{excerpt}」というやり取りが次へ持ち越された。"
            return f"「{excerpt}」というやり取りが次へ持ち越された。"
        if ctx.participant_names and ctx.place_id:
            return f"{'、'.join(ctx.participant_names)}のやり取りが{ctx.place_id}に残った。"
        if ctx.participant_names:
            return f"{'、'.join(ctx.participant_names)}のやり取りが次へ持ち越された。"
        if ctx.hook_summaries:
            return f"{ctx.hook_summaries[0]}が残ったまま場面が閉じた。"
        if ctx.tension_summaries:
            return f"{ctx.tension_summaries[0]}を残して場面が閉じた。"
        if ctx.place_id:
            return f"{ctx.place_id}の場面が次へ持ち越された。"
        return "閉じた場面の余韻が残った。"

    @staticmethod
    def _build_episode_close_fallback(ctx: NovelGeneratorContext) -> dict[str, Any] | None:
        summary = str(ctx.episode_summary or "").strip()
        goal = str(ctx.episode_goal or "").strip()
        episode_type = str(ctx.episode_type or "episode").strip()
        if not summary and not goal:
            return None
        title_source = goal or summary or "エピソードの区切り"
        title = title_source[:20]
        compact_summary = (summary or goal)[:50]
        prose_bits = [bit for bit in [summary, goal, *ctx.recent_log_lines[-2:]] if str(bit).strip()]
        prose = " ".join(prose_bits)[:220]
        if not prose:
            prose = f"{episode_type} の流れが次の場面へ持ち越された。"
        return {
            "title": title,
            "summary": compact_summary,
            "prose": prose,
        }

    @staticmethod
    def _parse_prose_response(text: str) -> dict[str, Any] | None:
        """LLM 応答テキストから散文データをパースする。"""
        cleaned = re.sub(r"```(?:json)?\s*", "", text).replace("```", "").strip()
        data: Any = None
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group())
                except json.JSONDecodeError:
                    pass

        if not isinstance(data, dict):
            return None

        required_keys = {"title", "summary", "prose"}
        if not required_keys.issubset(data.keys()):
            return None

        return data

    @staticmethod
    def _build_scene_close_fallback(
        ctx: NovelGeneratorContext,
        *,
        existing_scene_arc: dict[str, Any] | None = None,
    ) -> dict[str, str] | None:
        """scene_close parse failure 時の deterministic fallback を返す。"""
        if ctx.trigger != "scene_close":
            return None
        title = str(existing_scene_arc.get("title") or "").strip() if existing_scene_arc is not None else ""
        summary = str(existing_scene_arc.get("summary") or "").strip() if existing_scene_arc is not None else ""
        if not summary:
            summary = ctx.scene_outcome_summary or "閉じた場面の余韻が残った。"
        dialogue_excerpt = " ".join(line.split(": ", 1)[-1] for line in ctx.recent_log_lines[-2:])
        prose_parts = [summary]
        if ctx.participant_names:
            prose_parts.append(f"{'、'.join(ctx.participant_names)}の声がまだ場に残っている。")
        if dialogue_excerpt:
            prose_parts.append(dialogue_excerpt)
        prose = " ".join(part for part in prose_parts if part).strip()
        return {
            "title": (title or f"{ctx.place_id or '場面'}の余韻")[:20],
            "summary": summary[:50],
            "prose": prose[:220],
        }

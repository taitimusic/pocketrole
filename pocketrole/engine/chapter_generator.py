"""engine/chapter_generator.py — Phase 4 半自動 Chapter 生成

管理者がテーマを指定すると、LLM が beat 構造（description / goal / events）を
自動生成する。生成結果は chapter_proposals テーブルに保存され、管理者の承認後に
pending chapter として activate できる。
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from db.db_manager import DatabaseManager

if TYPE_CHECKING:
    from engine.llm.router import LLMRouter

logger = logging.getLogger(__name__)

_BEAT_PHASES = ("setup", "complication", "turning_point", "resolution")


class ChapterGenerationError(Exception):
    """Chapter の自動生成に失敗した場合に raise される。"""


class ChapterGenerator:
    """LLM を使って Chapter の beat 構造を自動生成する。

    StoryEngine.initialize() でインスタンス化・注入される。
    """

    def __init__(
        self,
        story_id: str,
        db: DatabaseManager,
        llm_router: "LLMRouter",
        llm_provider: str,
        llm_model: str,
        *,
        default_beat_timeout: int = 20,
        include_conflict_seeds: bool = True,
        max_events_per_beat: int = 2,
    ) -> None:
        self._story_id = story_id
        self._db = db
        self._llm_router = llm_router
        self._llm_provider = llm_provider
        self._llm_model = llm_model
        self._default_beat_timeout = default_beat_timeout
        self._include_conflict_seeds = include_conflict_seeds
        self._max_events_per_beat = max_events_per_beat

    async def generate_proposal(
        self,
        theme: str,
        turn_number: int,
        *,
        persona: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """テーマから chapter proposal を生成して DB に保存する。

        Args:
            theme: 管理者が指定するテーマ文字列（例: "文化祭"）
            turn_number: 現在の turn 番号（提案の文脈として使用）
            persona: active な director persona dict（あれば美学を反映）

        Returns:
            DB に保存した proposal record（id フィールドを含む）

        Raises:
            ChapterGenerationError: LLM 失敗またはレスポンスのパース・検証失敗
        """
        # 1. DB から conflict seed 用データを取得
        conflict_seeds: list[str] = []
        if self._include_conflict_seeds:
            try:
                relationships = await self._db.get_relationship_snapshots(self._story_id)
                tensions = await self._db.get_active_tensions(self._story_id)
                conflict_seeds = self.extract_conflict_seeds(relationships, tensions)
            except Exception as exc:
                logger.warning(
                    "conflict seed extraction failed, continuing without seeds",
                    extra={"story_id": self._story_id, "error": str(exc)},
                )

        # 2. LLM に chapter JSON を生成させる
        chapter_dict = await self._generate_with_llm(theme, conflict_seeds, persona)

        # 3. レスポンス検証
        self._validate_chapter_dict(chapter_dict)

        # 4. events_json をトリミング
        for beat in chapter_dict.get("beats", []):
            events = beat.get("events_json") or []
            beat["events_json"] = events[: self._max_events_per_beat]

        # 5. DB に保存
        persona_id = str(persona.get("persona_id", "")) if persona else None
        proposal_data = {
            "theme": theme,
            "proposed_at_turn": turn_number,
            "proposed_chapter_json": chapter_dict,
            "conflict_seeds_json": conflict_seeds,
            "generated_by_persona_id": persona_id,
        }
        proposal_id = await self._db.insert_chapter_proposal(self._story_id, proposal_data)

        # 6. 保存した record を返す
        saved = await self._db.get_chapter_proposal(proposal_id)
        assert saved is not None
        logger.info(
            "Chapter proposal generated",
            extra={
                "story_id": self._story_id,
                "proposal_id": proposal_id,
                "theme": theme,
                "turn_number": turn_number,
            },
        )
        return saved

    def extract_conflict_seeds(
        self,
        relationships: list[dict[str, Any]],
        tensions: list[dict[str, Any]] | None = None,
    ) -> list[str]:
        """relationship snapshot と tensions から conflict seed を deterministic に抽出する。

        Args:
            relationships: `get_relationship_snapshots()` の返り値
            tensions: `get_active_tensions()` の返り値（optional）

        Returns:
            最大 5 件の seed 文字列リスト
        """
        seeds: list[str] = []

        # relationship の trust 値から種を生成
        for rel in relationships:
            trust = float(rel.get("trust", 0.5))
            char_from = str(rel.get("char_id_from", ""))
            char_to = str(rel.get("char_id_to", ""))
            pair = f"{char_from}と{char_to}" if char_from and char_to else "キャラ間"
            if trust < 0.3:
                seeds.append(f"{pair}の強い不信感")
            elif trust < 0.45:
                seeds.append(f"{pair}の低い信頼度")
            if len(seeds) >= 3:
                break

        # active tensions から種を生成
        for tension in (tensions or [])[:3]:
            desc = str(tension.get("description", "")).strip()
            t_type = str(tension.get("tension_type", "")).strip()
            if desc:
                seeds.append(desc[:50])
            elif t_type:
                seeds.append(t_type)
            if len(seeds) >= 5:
                break

        return seeds[:5]

    # ── private ──────────────────────────────────────────────────────────────

    async def _generate_with_llm(
        self,
        theme: str,
        conflict_seeds: list[str],
        persona: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """LLM に chapter JSON を生成させる。"""
        system_prompt = (
            "あなたはストーリープランナーです。"
            "指定されたテーマと関係性の情報を元に、"
            "4 フェーズ（setup / complication / turning_point / resolution）の"
            "chapter 定義を生成してください。"
        )

        persona_info = ""
        if persona:
            values = persona.get("values_json", [])
            traits = persona.get("traits_json", [])
            persona_info = (
                f"\n監督の美学 — values: {values}, traits: {traits}\n"
            )

        seeds_info = ""
        if conflict_seeds:
            seeds_info = f"\nconflict seeds: {', '.join(conflict_seeds)}\n"

        timestamp = int(time.time())
        beat_events_hint = (
            f"（events_json は各 beat につき最大 {self._max_events_per_beat} 件）"
        )

        user_prompt = (
            f"テーマ: {theme}{seeds_info}{persona_info}\n"
            "以下の JSON 形式のみで回答してください（余分なテキスト不可）:\n"
            "{\n"
            f'  "chapter_id": "auto_{timestamp}",\n'
            '  "title": "チャプタータイトル",\n'
            '  "theme": "テーマ（1行）",\n'
            '  "world_injection": "世界観注入テキスト（2〜3文）",\n'
            '  "start_condition": "manual",\n'
            "  \"beats\": [\n"
            '    {"phase": "setup",         "description": "...", "goal": "...", '
            '"events_json": [{"type": "...", "desc": "..."}]},\n'
            '    {"phase": "complication",  "description": "...", "goal": "...", "events_json": [...]},\n'
            '    {"phase": "turning_point", "description": "...", "goal": "...", "events_json": [...]},\n'
            '    {"phase": "resolution",    "description": "...", "goal": "...", "events_json": [...]}\n'
            f"  ]\n"
            f"}}\n{beat_events_hint}"
        )

        try:
            response = await self._llm_router.generate(
                provider=self._llm_provider,
                model=self._llm_model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.7,
                max_tokens=800,
                request_tag=f"chapter_generator:{theme or 'auto'}",
            )
        except Exception as exc:
            raise ChapterGenerationError(f"LLM call failed: {exc}") from exc

        data = _parse_json_response(response.text)
        if data is None:
            raise ChapterGenerationError(
                f"LLM response could not be parsed as JSON: {response.text[:200]}"
            )
        return data

    def _validate_chapter_dict(self, chapter: dict[str, Any]) -> None:
        """生成された chapter dict を検証する。不正なら ChapterGenerationError を raise。"""
        if not str(chapter.get("chapter_id", "")).strip():
            raise ChapterGenerationError("chapter_id is missing or empty")
        if not str(chapter.get("title", "")).strip():
            raise ChapterGenerationError("title is missing or empty")
        if not str(chapter.get("world_injection", "")).strip():
            raise ChapterGenerationError("world_injection is missing or empty")
        beats = chapter.get("beats")
        if not isinstance(beats, list) or len(beats) != 4:
            raise ChapterGenerationError(
                f"beats must have exactly 4 phases, got: {len(beats) if isinstance(beats, list) else type(beats)}"
            )
        for beat in beats:
            phase = beat.get("phase", "")
            if phase not in _BEAT_PHASES:
                raise ChapterGenerationError(f"invalid beat phase: {phase!r}")


# ------------------------------------------------------------------
# module-level helper
# ------------------------------------------------------------------

def _parse_json_response(text: str) -> dict[str, Any] | None:
    """LLM レスポンスから JSON オブジェクトを抽出してパースする。

    ネストが深い chapter JSON に対応するため、まず全体を直接パースし、
    次に最初の '{' から最後の '}' までの greedy 抽出を試みる。
    """
    cleaned = re.sub(r"```(?:json)?\s*", "", text).replace("```", "").strip()
    # 1. まず cleaned 全体を直接 JSON としてパース
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    # 2. '{' から '}' の greedy 最大マッチを試みる
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group())
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return None

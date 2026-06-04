"""engine/chapter_manager.py — Chapter System ランタイム管理

YAML で定義されたチャプターを DB から読み込み、start_condition に基づいて
chapter を activate し、beat を deterministic または LLM-assisted で進行させ、
beat event を director_intervention として注入する。

beat 進行は timeout が基本。optional で LLM に goal 達成を判定させることができる。
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from db.db_manager import DatabaseManager

if TYPE_CHECKING:
    from engine.director_persona import DirectorPersona
    from engine.llm.router import LLMRouter

logger = logging.getLogger(__name__)

_BEAT_SEQUENCE: tuple[str, ...] = ("setup", "complication", "turning_point", "resolution")

# VALID_INTERVENTION_TYPES は story_director.py と同じセット（circular import 回避のためここで定義）
_VALID_INTERVENTION_TYPES: frozenset[str] = frozenset({
    "plot_twist", "mood", "relationship_catalyst",
    "revelation", "crisis", "opportunity",
})


class ChapterManager:
    """Chapter System のランタイム管理クラス。

    StoryEngine の _run_post_round_hooks() から毎ラウンド呼ばれる。
    chapter の起動・beat 進行・close を処理する。
    """

    def __init__(
        self,
        story_id: str,
        db: DatabaseManager,
        *,
        beat_check_interval_rounds: int = 1,
        beat_timeout_turns: int = 30,
        auto_event_injection: bool = True,
        director_persona: "DirectorPersona | None" = None,
    ) -> None:
        self._story_id = story_id
        self._db = db
        self._beat_check_interval = beat_check_interval_rounds
        self._beat_timeout = beat_timeout_turns
        self._auto_inject = auto_event_injection
        self._director_persona = director_persona
        # Phase 3 optional: LLM-assisted beat 進行判定（story_engine.initialize() が後注入）
        self._llm_router: "LLMRouter | None" = None
        self._llm_provider: str = ""
        self._llm_model: str = ""
        self._enable_llm_beat_judgment: bool = False
        self._llm_beat_min_elapsed: int = 5
        # beat_id → turn when it became active（in-memory キャッシュ、initialize で復元）
        self._beat_start_turns: dict[int, int] = {}
        self._beat_intervention_ids: dict[int, list[int]] = {}

    async def initialize(self) -> None:
        """既存 active chapter の beat 開始 turn を in-memory に復元する。"""
        chapter = await self._db.get_active_chapter(self._story_id)
        if chapter is not None:
            await self._reconstruct_beat_start_turns(chapter)
            beats = await self._db.get_chapter_beats(chapter["id"])
            current_phase = str(chapter.get("current_beat") or "setup")
            current_beat = next((beat for beat in beats if beat["phase"] == current_phase), None)
            if current_beat is not None:
                await self._reconstruct_beat_intervention_ids(current_beat)
            logger.info(
                "ChapterManager initialized with active chapter",
                extra={
                    "story_id": self._story_id,
                    "chapter_id": chapter.get("chapter_id"),
                    "current_beat": chapter.get("current_beat"),
                },
            )

    async def process_round(self, turn_number: int) -> dict[str, Any]:
        """毎 round 呼ばれる。chapter 起動・beat 進行・close を処理する。

        Returns:
            {
                "activated_chapter": dict | None,   # 今ラウンドで activate した chapter
                "beat_advanced": bool,               # beat が進んだ
                "closed_chapter": dict | None,       # 今ラウンドで close した chapter
                "injected_interventions": list[int], # 注入した intervention ID リスト
            }
        """
        result: dict[str, Any] = {
            "activated_chapter": None,
            "beat_advanced": False,
            "closed_chapter": None,
            "injected_interventions": [],
        }

        if turn_number % self._beat_check_interval != 0:
            return result

        chapter = await self._db.get_active_chapter(self._story_id)

        # ── pending chapter の起動チェック ──────────────────────────────────────
        if chapter is None:
            pending = await self._db.get_pending_chapters(self._story_id)
            for pending_ch in pending:
                condition = pending_ch.get("start_condition")
                if self._evaluate_start_condition(condition, turn_number):
                    chapter_db_id = int(pending_ch["id"])
                    await self._db.activate_chapter(chapter_db_id, turn_number)
                    chapter = await self._db.get_active_chapter(self._story_id)
                    result["activated_chapter"] = chapter
                    logger.info(
                        "Chapter activated",
                        extra={
                            "story_id": self._story_id,
                            "chapter_id": pending_ch.get("chapter_id"),
                            "turn_number": turn_number,
                        },
                    )
                    # 第 1 beat の開始 turn を記録し、event injection を実行
                    if chapter is not None:
                        beats = await self._db.get_chapter_beats(chapter["id"])
                        if beats:
                            first_beat = beats[0]
                            self._beat_start_turns[first_beat["id"]] = turn_number
                            if self._auto_inject:
                                ivs = await self._inject_beat_events(first_beat, turn_number)
                                result["injected_interventions"].extend(ivs)
                    break  # 1 ラウンドで activate するのは 1 chapter のみ

        # ── active chapter の beat 進行チェック ─────────────────────────────────
        if chapter is None:
            return result

        beats = await self._db.get_chapter_beats(chapter["id"])
        current_phase = str(chapter.get("current_beat") or "setup")
        current_beat = next((b for b in beats if b["phase"] == current_phase), None)

        if current_beat is None:
            logger.warning(
                "Current beat not found in beats list",
                extra={
                    "story_id": self._story_id,
                    "chapter_id": chapter.get("chapter_id"),
                    "current_phase": current_phase,
                },
            )
            return result

        beat_id = int(current_beat["id"])
        beat_start = self._beat_start_turns.get(beat_id, int(chapter.get("opened_turn") or 0))
        timed_out = (turn_number - beat_start) >= self._beat_timeout
        beat_completed = await self._beat_events_completed(current_beat)
        if not beat_completed:
            beat_completed = await self._beat_goal_hooks_resolved(current_beat, beat_start)

        # Phase 3 optional: LLM による beat goal 達成判定（deterministic 条件未達時のみ）
        if (
            not beat_completed
            and not timed_out
            and self._enable_llm_beat_judgment
            and self._llm_router is not None
            and (turn_number - beat_start) >= self._llm_beat_min_elapsed
        ):
            beat_completed = await self._judge_beat_with_llm(current_beat, beat_start, turn_number)

        if not beat_completed and not timed_out:
            return result

        # ── beat 進行 ────────────────────────────────────────────────────────────
        next_phase = self._next_beat_phase(current_phase)
        await self._db.update_beat_status(beat_id, "reached", reached_turn=turn_number)
        logger.info(
            "Beat reached",
            extra={
                "story_id": self._story_id,
                "chapter_id": chapter.get("chapter_id"),
                "phase": current_phase,
                "turn_number": turn_number,
            },
        )

        if next_phase is None:
            # 全 beat 完了 → chapter close
            await self._db.close_chapter(
                int(chapter["id"]),
                closed_turn=turn_number,
                reason="completed",
                carry_over={},
            )
            result["closed_chapter"] = chapter
            logger.info(
                "Chapter completed",
                extra={
                    "story_id": self._story_id,
                    "chapter_id": chapter.get("chapter_id"),
                    "turn_number": turn_number,
                },
            )
        else:
            await self._db.update_chapter_beat(int(chapter["id"]), next_phase)
            result["beat_advanced"] = True
            next_beat = next((b for b in beats if b["phase"] == next_phase), None)
            if next_beat is not None:
                next_beat_id = int(next_beat["id"])
                self._beat_start_turns[next_beat_id] = turn_number
                if self._auto_inject:
                    ivs = await self._inject_beat_events(next_beat, turn_number)
                    result["injected_interventions"].extend(ivs)
            logger.info(
                "Beat advanced",
                extra={
                    "story_id": self._story_id,
                    "chapter_id": chapter.get("chapter_id"),
                    "from_phase": current_phase,
                    "to_phase": next_phase,
                    "turn_number": turn_number,
                },
            )

        return result

    async def get_active_chapter(self) -> dict[str, Any] | None:
        """現在 active な chapter を返す。"""
        return await self._db.get_active_chapter(self._story_id)

    async def get_current_world_injection(self) -> str | None:
        """active chapter の world_injection テキストを返す。なければ None。"""
        chapter = await self._db.get_active_chapter(self._story_id)
        if chapter is None:
            return None
        injection = chapter.get("world_injection")
        return str(injection) if injection else None

    async def get_current_beat(self) -> dict[str, Any] | None:
        """active chapter の現在 beat record を返す。"""
        chapter = await self._db.get_active_chapter(self._story_id)
        if chapter is None:
            return None
        beats = await self._db.get_chapter_beats(chapter["id"])
        current_phase = str(chapter.get("current_beat") or "setup")
        return next((b for b in beats if b["phase"] == current_phase), None)

    # ── private helpers ──────────────────────────────────────────────────────────

    def _evaluate_start_condition(self, condition: str | None, turn_number: int) -> bool:
        """start_condition 文字列を評価して bool を返す。

        対応フォーマット:
        - None / 空文字 → 即起動（True）
        - "manual"       → 自動起動しない（False）
        - "turn >= N"    → turn_number >= N のとき True
        """
        if not condition:
            return True
        cond = condition.strip()
        if cond == "manual":
            return False
        if cond.startswith("turn >= "):
            try:
                threshold = int(cond[8:])
                return turn_number >= threshold
            except ValueError:
                logger.warning("Invalid start_condition format: %s", condition)
                return False
        logger.warning("Unknown start_condition format: %s", condition)
        return False

    def _next_beat_phase(self, current: str) -> str | None:
        """_BEAT_SEQUENCE における次の phase を返す。最後なら None。"""
        try:
            idx = _BEAT_SEQUENCE.index(current)
        except ValueError:
            return None
        if idx >= len(_BEAT_SEQUENCE) - 1:
            return None
        return _BEAT_SEQUENCE[idx + 1]

    async def _inject_beat_events(
        self, beat: dict[str, Any], turn_number: int
    ) -> list[int]:
        """beat の events_json を director_interventions として挿入する。"""
        events: list[dict[str, Any]] = beat.get("events_json") or []
        injected: list[int] = []
        selected_events = self._select_beat_events(events)

        # v2 upgrade: director_persona の steering signals から intervention_type を決定
        intervention_type = _select_intervention_type(
            self._director_persona.get_steering_signals().intervention_type_weights
            if self._director_persona is not None
            else {}
        )

        for event in selected_events:
            desc = str(event.get("desc") or event.get("description") or "").strip()
            if not desc:
                continue
            iv_id = await self._db.insert_intervention(
                self._story_id,
                {
                    "intervention_type": intervention_type,
                    "title": str(event.get("type", "chapter_event")),
                    "description": desc,
                    "prompt_injection": desc,
                    "scope": "all",
                    "active_from_turn": turn_number,
                    "active_until_turn": turn_number + self._beat_timeout,
                    "status": "active",
                },
            )
            injected.append(iv_id)
        self._beat_intervention_ids[int(beat["id"])] = injected
        return injected

    async def _judge_beat_with_llm(
        self,
        beat: dict[str, Any],
        beat_start: int,
        turn_number: int,
    ) -> bool:
        """LLM に beat goal の達成を判定させる。

        Returns:
            True  → 達成と判定（beat を進行させる）
            False → 未達成 or 判定失敗（timeout フォールバック）
        """
        if self._llm_router is None:
            return False
        goal = str(beat.get("goal") or "").strip()
        if not goal:
            return False
        try:
            elapsed = turn_number - beat_start
            system_prompt = (
                "あなたはストーリーの beat 進行判定を行うアシスタントです。"
                "beat の goal が達成されたか判定してください。"
            )
            user_prompt = (
                f"beat phase: {beat.get('phase')}\n"
                f"beat goal: {goal}\n"
                f"beat description: {beat.get('description', '')}\n"
                f"経過ターン数: {elapsed} / 最大タイムアウト: {self._beat_timeout}\n\n"
                "このゴールは達成されましたか？\n"
                "以下の JSON 形式のみで回答してください:\n"
                '{"achieved": true か false, "confidence": 0.0から1.0, "reason": "理由"}'
            )
            response = await self._llm_router.generate(
                provider=self._llm_provider,
                model=self._llm_model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                temperature=0.1,
                max_tokens=100,
                request_tag=f"chapter_manager:beat_judge:{beat.get('phase', 'unknown')}",
            )
            data = _parse_json_response(response.text)
            if data is None:
                return False
            achieved = bool(data.get("achieved", False))
            confidence = float(data.get("confidence", 0.0))
            if achieved and confidence >= 0.7:
                logger.info(
                    "LLM judged beat as achieved",
                    extra={
                        "story_id": self._story_id,
                        "phase": beat.get("phase"),
                        "turn_number": turn_number,
                        "confidence": confidence,
                        "reason": str(data.get("reason", "")),
                    },
                )
                return True
        except Exception as exc:
            logger.warning(
                "LLM beat judgment failed",
                extra={"story_id": self._story_id, "error": str(exc)},
            )
        return False

    async def _reconstruct_beat_start_turns(self, chapter: dict[str, Any]) -> None:
        """既存 active chapter の beat 開始 turn を in-memory に復元する。

        beats を順に見て:
        - status="reached" の beat: 次 beat の start = そのビートの reached_turn
        - それ以降（現在 active の beat）: start = 前 beat の reached_turn or opened_turn
        """
        beats = await self._db.get_chapter_beats(chapter["id"])
        prev_turn = int(chapter.get("opened_turn") or 0)
        for beat in beats:
            beat_id = int(beat["id"])
            if beat.get("status") == "reached" and beat.get("reached_turn") is not None:
                self._beat_start_turns[beat_id] = prev_turn
                prev_turn = int(beat["reached_turn"])
            else:
                # 現在 active（または pending）の beat はここで start turn を設定して終了
                self._beat_start_turns[beat_id] = prev_turn
                break

    async def _reconstruct_beat_intervention_ids(self, beat: dict[str, Any]) -> None:
        """現 beat に紐づく intervention IDs を title/description/turn から復元する。"""
        beat_id = int(beat["id"])
        beat_start = self._beat_start_turns.get(beat_id)
        if beat_start is None:
            return
        events: list[dict[str, Any]] = beat.get("events_json") or []
        if not events:
            self._beat_intervention_ids[beat_id] = []
            return
        candidates = await self._db.get_interventions_started_on_turn(
            self._story_id,
            active_from_turn=beat_start,
        )
        matched_ids: list[int] = []
        for event in self._select_beat_events(events):
            desc = str(event.get("desc") or event.get("description") or "").strip()
            title = str(event.get("type") or "chapter_event")
            for intervention in candidates:
                if int(intervention["id"]) in matched_ids:
                    continue
                if (
                    str(intervention.get("title") or "") == title
                    and str(intervention.get("description") or "").strip() == desc
                ):
                    matched_ids.append(int(intervention["id"]))
                    break
        self._beat_intervention_ids[beat_id] = matched_ids

    async def _beat_events_completed(self, beat: dict[str, Any]) -> bool:
        """beat event が acknowledged/resolved/expired に到達していれば True。"""
        events: list[dict[str, Any]] = beat.get("events_json") or []
        if not events:
            return False
        beat_id = int(beat["id"])
        intervention_ids = self._beat_intervention_ids.get(beat_id)
        if intervention_ids is None:
            await self._reconstruct_beat_intervention_ids(beat)
            intervention_ids = self._beat_intervention_ids.get(beat_id, [])
        if not intervention_ids:
            return False
        interventions = await self._db.get_interventions_by_ids(intervention_ids)
        if len(interventions) < len(intervention_ids):
            return False
        terminal_statuses = {"acknowledged", "resolved", "expired"}
        return all(str(intervention.get("status") or "") in terminal_statuses for intervention in interventions)

    async def _beat_goal_hooks_resolved(self, beat: dict[str, Any], beat_start: int) -> bool:
        """beat goal と対応する hook が resolved なら True。"""
        goal = str(beat.get("goal") or "").strip()
        keywords = self._goal_keywords(goal)
        if not keywords:
            return False
        resolved_hooks = await self._db.get_resolved_story_hooks(
            self._story_id,
            since_turn=beat_start,
            limit=20,
        )
        for hook in resolved_hooks:
            haystack = " ".join(
                [
                    str(hook.get("title") or "").strip(),
                    str(hook.get("description") or "").strip(),
                    str(hook.get("resolution_summary") or "").strip(),
                ]
            )
            if all(keyword in haystack for keyword in keywords):
                return True
        return False

    def _goal_keywords(self, goal: str) -> list[str]:
        """goal から deterministic に主要語を取り出す。"""
        if not goal:
            return []
        tokens = [
            token.strip()
            for token in re.split(r"[\s、。！？!?,:：;；/／（）()「」『』]+", goal)
            if token.strip()
        ]
        keywords: list[str] = []
        for token in tokens:
            if len(token) >= 2 and token not in keywords:
                keywords.append(token)
        if not keywords:
            keywords.append(goal)
        return keywords

    def _select_beat_events(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """beat event 候補から persona 選好に合う最大 1 件を選ぶ。"""
        if not events:
            return []
        if len(events) == 1:
            return [events[0]]
        preferred_flavors = (
            self._director_persona.get_steering_signals().preferred_event_flavors
            if self._director_persona is not None
            else []
        )
        for flavor in preferred_flavors:
            for event in events:
                event_flavor = str(event.get("flavor") or "generic").strip()
                if event_flavor == flavor:
                    return [event]
        return [events[0]]


# ------------------------------------------------------------------
# module-level helper
# ------------------------------------------------------------------

def _parse_json_response(text: str) -> dict[str, Any] | None:
    """LLM レスポンスから JSON オブジェクトを抽出してパースする。"""
    cleaned = re.sub(r"```(?:json)?\s*", "", text).replace("```", "").strip()
    for pattern in (r"\{[^{}]*\}", r"\{.*\}"):
        m = re.search(pattern, cleaned, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    return None


def _select_intervention_type(weights: dict[str, float]) -> str:
    """steering weights から VALID_INTERVENTION_TYPES の中で最適な type を選ぶ。

    weights が空 or 全キーが無効な場合は "plot_twist" を返す。
    """
    if not weights:
        return "plot_twist"
    valid = {k: v for k, v in weights.items() if k in _VALID_INTERVENTION_TYPES}
    if not valid:
        return "plot_twist"
    return max(valid, key=lambda t: valid[t])

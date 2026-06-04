from __future__ import annotations

import logging
import random
from typing import Any

logger = logging.getLogger(__name__)


class Scheduler:
    """発言スケジューラー（DB・LLM 依存なし、同期・純粋計算クラス）。

    同一場所のキャラクター数と会話文脈から発言種別（monologue / reply / group）を決定する。
    monologue_solo_rate / monologue_group_rate を設定すると、他キャラが存在する場合でも
    確率的に内省モノローグターンを注入し、「どうするの？」型の質問ループを防ぐ。
    """

    def __init__(
        self,
        reply_trust_threshold: float = 0.6,
        monologue_solo_rate: float = 0.0,
        monologue_group_rate: float = 0.0,
        advance_monologue_bonus: float = 0.0,
    ) -> None:
        self._threshold = reply_trust_threshold
        self._monologue_solo_rate = monologue_solo_rate
        self._monologue_group_rate = monologue_group_rate
        self._advance_monologue_bonus = advance_monologue_bonus

    def schedule_utterance(
        self,
        char_id: str,
        same_place_chars: list[str],
        relationships: dict[tuple[str, str], dict[str, Any]],
        anomaly: dict[str, Any] | None = None,
        *,
        has_recent_dialogue: bool = False,
        in_active_scene_or_session: bool = False,
        speaker_intent: str | None = None,
    ) -> dict[str, Any]:
        """発言ターン種別を決定して返す。

        Args:
            char_id: 発言するキャラクターの ID
            same_place_chars: 同じ場所にいる他キャラクター ID のリスト（自分自身は含まない）
            relationships: (from_id, to_id) → {"trust": float, ...} の辞書
            anomaly: 異常情報（意思決定には影響しない）

        Returns:
            {
                "msg_type": "monologue" | "reply" | "group",
                "target_char_id": str | list[str] | None,
                "reason": str,
            }
        """
        if anomaly is not None:
            logger.debug(
                "schedule_utterance: anomaly detected for %s: %s",
                char_id,
                anomaly.get("label", ""),
            )

        count = len(same_place_chars)

        # ── 確率的モノローグ注入 ──────────────────────────────────────────
        # 会話の流れが続いている(has_recent_dialogue)場合や
        # reactive な intent ("react") の場合は対話を優先してモノローグを注入しない。
        if not has_recent_dialogue and speaker_intent not in ("react",):
            base_rate = 0.0
            if count == 1:
                base_rate = self._monologue_solo_rate
            elif count >= 2:
                base_rate = self._monologue_group_rate
            if speaker_intent == "advance":
                base_rate = min(1.0, base_rate + self._advance_monologue_bonus)
            if base_rate > 0.0 and random.random() < base_rate:
                reason = f"内省モノローグ注入（{count}人同席中・確率{base_rate:.0%}）"
                logger.debug("char=%s %s", char_id, reason)
                return {"msg_type": "monologue", "target_char_id": None, "reason": reason}

        # ── 既存のロジック ────────────────────────────────────────────────
        if count >= 2:
            if has_recent_dialogue or speaker_intent == "react":
                partner = same_place_chars[0]
                reason = "会話文脈があるため multi-party でも返答を優先"
                logger.debug("char=%s %s", char_id, reason)
                return {
                    "msg_type": "reply",
                    "target_char_id": partner,
                    "reason": reason,
                }
            reason = f"グループ会話（{count}人と同じ場所）"
            logger.debug("char=%s %s", char_id, reason)
            return {
                "msg_type": "group",
                "target_char_id": list(same_place_chars),
                "reason": reason,
            }

        if count == 1:
            partner = same_place_chars[0]
            reason = f"同席相手 {partner} がいるため返答を優先"
            logger.debug("char=%s %s", char_id, reason)
            return {
                "msg_type": "reply",
                "target_char_id": partner,
                "reason": reason,
            }

        # count == 0
        reason = "独り言（周囲に誰もいない）"
        logger.debug("char=%s %s", char_id, reason)
        return {
            "msg_type": "monologue",
            "target_char_id": None,
            "reason": reason,
        }

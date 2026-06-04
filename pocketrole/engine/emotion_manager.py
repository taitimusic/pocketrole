from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# 感情キー（一覧）
EMOTION_KEYS: tuple[str, ...] = ("stress", "motivation", "loneliness", "excitement")

# 中立値（自然減衰の収束先）
NEUTRAL = 0.5

# 感情トリガー → emotion_delta マッピング
EMOTION_TRIGGERS: dict[str, dict[str, float]] = {
    "moved":            {"stress": -0.1, "motivation": +0.05},
    "talked_to":        {"loneliness": -0.2},
    "alone_long":       {"loneliness": +0.15, "motivation": -0.1},
    "anomaly_detected": {"stress": +0.2},
    "event_day":        {"excitement": +0.3},
    "memory_recall":    {"motivation": +0.1},
    "secret_unlocked":  {"motivation": +0.2},
}


class EmotionManager:
    """感情値の計算・管理を行う純粋計算クラス。DB・LLM 依存なし。"""

    @staticmethod
    def clamp(value: float) -> float:
        """値を 0.0〜1.0 の範囲にクランプして返す。"""
        return max(0.0, min(1.0, value))

    def clamp_emotions(self, emotions: dict[str, float]) -> dict[str, float]:
        """全感情キーをクランプした新しい dict を返す（元の dict を破壊しない）。"""
        return {key: self.clamp(val) for key, val in emotions.items()}

    def apply_trigger(
        self,
        emotions: dict[str, float],
        trigger_type: str,
        extra_delta: dict[str, float] | None = None,
    ) -> dict[str, float]:
        """感情トリガーを適用して新しい感情値の dict を返す。

        Args:
            emotions: 現在の感情値 dict
            trigger_type: トリガー種別（EMOTION_TRIGGERS のキー）
            extra_delta: 追加デルタ（例: 信頼度ボーナス）

        Returns:
            クランプ適用済みの新しい感情値 dict
        """
        delta = EMOTION_TRIGGERS.get(trigger_type)
        if delta is None:
            logger.warning("Unknown trigger_type: %s", trigger_type)
            return self.clamp_emotions(emotions)

        result = dict(emotions)
        for key, d in delta.items():
            if key in result:
                result[key] = result[key] + d

        if extra_delta:
            for key, d in extra_delta.items():
                if key in result:
                    result[key] = result[key] + d

        return self.clamp_emotions(result)

    def apply_decay(
        self,
        emotions: dict[str, float],
        decay_rate: float = 0.05,
    ) -> dict[str, float]:
        """各感情値を中立値（0.5）に向けて減衰させた新しい dict を返す。

        計算式: new_value = current + (0.5 - current) * decay_rate

        Args:
            emotions: 現在の感情値 dict
            decay_rate: 減衰率（デフォルト 0.05）

        Returns:
            クランプ適用済みの新しい感情値 dict
        """
        result = {
            key: val + (NEUTRAL - val) * decay_rate
            for key, val in emotions.items()
        }
        return self.clamp_emotions(result)

    def judge_expression(self, emotions: dict[str, float]) -> str:
        """感情値から表情ラベルを優先度順に判定して返す。

        Returns:
            表情ラベル文字列（"happy" / "angry" / "sad" / "surprised" /
            "worried" / "content" / "lonely" / "tired" / "neutral"）
        """
        stress = emotions.get("stress", NEUTRAL)
        motivation = emotions.get("motivation", NEUTRAL)
        loneliness = emotions.get("loneliness", NEUTRAL)
        excitement = emotions.get("excitement", NEUTRAL)

        if motivation > 0.7 and excitement > 0.6:
            return "happy"
        if stress > 0.7:
            return "angry"
        if loneliness > 0.6 and motivation < 0.4:
            return "sad"
        if excitement > 0.8:
            return "surprised"
        if stress > 0.5 and motivation < 0.5:
            return "worried"
        if motivation > 0.6 and excitement < 0.3:
            return "content"
        if loneliness > 0.5:
            return "lonely"
        if motivation < 0.3:
            return "tired"
        return "neutral"

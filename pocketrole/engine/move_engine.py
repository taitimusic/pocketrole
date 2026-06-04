from __future__ import annotations

import logging
import random
from typing import Any

from engine.place_manager import PlaceManager

logger = logging.getLogger(__name__)


class MoveEngine:
    """キャラクターの自律移動ロジック。DB・LLM 依存なし（純粋計算クラス）。"""

    def __init__(
        self,
        place_manager: PlaceManager,
        base_move_prob: float = 0.3,
        rng: random.Random | None = None,
    ) -> None:
        self._pm = place_manager
        self._base_prob = base_move_prob
        self._rng = rng or random.Random()

    def calc_move_probability(
        self,
        emotions: dict[str, float],
        current_place: str,
        recommended_place: str | None,
        *,
        same_place_count: int = 0,
        current_place_pull_strength: float = 0.0,
        social_target_place: str | None = None,
        social_pull_strength: float = 0.0,
    ) -> float:
        """移動確率を 0.0〜1.0 の float で返す。"""
        base = self._base_prob

        # 感情補正（各感情の中立値 0.5 からの偏差）
        delta = 0.0
        delta += (emotions.get("motivation", 0.5) - 0.5) * 0.4   # 高い → 行動意欲UP
        delta += (emotions.get("stress", 0.5) - 0.5) * 0.2        # 高い → 逃避行動UP
        delta += (emotions.get("loneliness", 0.5) - 0.5) * 0.2    # 高い → 誰かを求めてUP

        # スケジュール補正
        if recommended_place is not None:
            if recommended_place != current_place:
                delta += 0.3   # スケジュール通りでない場所にいる → 大幅UP
            else:
                delta -= 0.2   # スケジュール通りの場所にいる → 留まりやすい

        if same_place_count > 0:
            delta -= 0.15

        if current_place_pull_strength > 0.0:
            delta -= 0.20 * max(0.0, current_place_pull_strength)

        if social_target_place is not None and same_place_count == 0:
            delta += 0.20 * max(0.0, social_pull_strength)

        return max(0.0, min(1.0, base + delta))

    def should_move(
        self,
        emotions: dict[str, float],
        current_place: str,
        recommended_place: str | None,
        *,
        same_place_count: int = 0,
        current_place_pull_strength: float = 0.0,
        social_target_place: str | None = None,
        social_pull_strength: float = 0.0,
    ) -> bool:
        """移動するかを確率的に判定する。"""
        prob = self.calc_move_probability(
            emotions,
            current_place,
            recommended_place,
            same_place_count=same_place_count,
            current_place_pull_strength=current_place_pull_strength,
            social_target_place=social_target_place,
            social_pull_strength=social_pull_strength,
        )
        return self._rng.random() < prob

    def decide_destination(
        self,
        current_place: str,
        recommended_place: str | None,
        *,
        social_target_place: str | None = None,
    ) -> str:
        """移動先を決定する。隣接制約を厳格に適用。"""
        adjacent = self._pm.get_adjacent_places(current_place)
        if not adjacent:
            logger.debug("No adjacent places for %s — staying put", current_place)
            return current_place  # 移動不可

        if social_target_place and social_target_place in adjacent:
            return social_target_place

        if recommended_place and recommended_place in adjacent:
            return recommended_place  # スケジュール優先

        return self._rng.choice(adjacent)  # ランダムに隣接場所を選択

    def generate_move_reason(
        self,
        current_place: str,
        destination: str,
        emotions: dict[str, float],
        recommended_place: str | None,
    ) -> str:
        """移動理由のテキストをルールベースで生成。LLM 不使用。"""
        dest_label = self._pm.get_place_label(destination)
        from_label = self._pm.get_place_label(current_place)

        # スケジュール追従
        if recommended_place == destination:
            return f"スケジュールに従い{dest_label}へ向かう"

        # 感情優先度: stress > loneliness > motivation > default
        stress = emotions.get("stress", 0.5)
        loneliness = emotions.get("loneliness", 0.5)
        motivation = emotions.get("motivation", 0.5)

        if stress > 0.7:
            return f"気分転換に{dest_label}へ向かう"
        if loneliness > 0.7:
            return f"誰かに会いたくて{dest_label}へ向かう"
        if motivation > 0.7:
            return f"やる気に溢れて{dest_label}へ向かう"
        return f"{from_label}から{dest_label}へ移動する"

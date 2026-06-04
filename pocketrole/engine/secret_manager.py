from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# 開示レベルの段階比率
HINT_STAGE_1_RATIO = 0.7  # 閾値の70%以上でレベル1ヒント
HINT_STAGE_2_RATIO = 0.9  # 閾値の90%以上でレベル2ヒント


class SecretManager:
    """秘密開示判定クラス（DB・LLM 依存なし、同期・純粋計算クラス）。

    secret_reveal_condition フリーテキストから信頼度閾値を正規表現で抽出し、
    同一場所にいるパートナーとの信頼度に基づいて開示レベル（0〜3）を算出する。
    """

    def _extract_trust_threshold(self, condition_text: str) -> float | None:
        """条件テキストから信頼度閾値を抽出する。

        例: "信頼度0.8以上のキャラクターと..." → 0.8
            "信頼度0.75以上かつ..." → 0.75

        Returns:
            float（閾値）または None（パターン未発見時）
        """
        m = re.search(r'信頼度(\d+(?:\.\d+)?)', condition_text)
        return float(m.group(1)) if m else None

    def calc_hint_level(
        self,
        trust_threshold: float,
        max_partner_trust: float,
    ) -> int:
        """信頼度の到達度から開示レベル（0〜3）を計算する。

        Args:
            trust_threshold: 秘密開示の信頼度閾値
            max_partner_trust: 同一場所パートナーの最大信頼度

        Returns:
            3: 完全開示（max_trust >= threshold）
            2: 強いヒント（max_trust >= threshold × 0.9）
            1: 淡いヒント（max_trust >= threshold × 0.7）
            0: ヒントなし
        """
        if max_partner_trust >= trust_threshold:
            return 3
        if max_partner_trust >= trust_threshold * HINT_STAGE_2_RATIO:
            return 2
        if max_partner_trust >= trust_threshold * HINT_STAGE_1_RATIO:
            return 1
        return 0

    def build_hint_text(self, secret_content: str, hint_level: int) -> str | None:
        """開示レベルに応じたプロンプト注入テキストを生成する。

        Args:
            secret_content: 秘密の内容テキスト
            hint_level: 開示レベル（0〜3）

        Returns:
            str（注入テキスト）または None（レベル0）
        """
        if hint_level == 0:
            return None
        if hint_level >= 3:
            return (
                f"【秘密解放】あなたは今、自分の秘密を打ち明ける気になっている。"
                f"秘密: {secret_content}"
            )
        if hint_level == 2:
            return "あなたは何か大切なことを打ち明けそうな雰囲気がある。"
        # level 1
        return "あなたの心の奥には、まだ誰にも話していないことがある。"

    def check_reveal(
        self,
        char: dict[str, Any],
        same_place_chars: list[str],
        relationships: dict[tuple[str, str], dict[str, Any]],
    ) -> dict[str, Any] | None:
        """秘密開示チェックのメインエントリーポイント。

        Args:
            char: characters テーブルの行辞書（"secret", "secret_reveal_condition", "id" を参照）
            same_place_chars: 同じ場所にいる他キャラクター ID のリスト（自分自身は含まない）
            relationships: (from_id, to_id) → {"trust": float, ...} の辞書

        Returns:
            {"hint_level": int, "hint_text": str} または None（秘密なし・閾値抽出不可・レベル0）
        """
        secret_content = char.get("secret")
        if not secret_content:
            return None

        condition_text = char.get("secret_reveal_condition") or ""
        trust_threshold = self._extract_trust_threshold(condition_text)
        if trust_threshold is None:
            logger.warning(
                "char=%s: 信頼度閾値を抽出できませんでした condition=%r",
                char.get("id"),
                condition_text,
            )
            return None

        char_id = char.get("id", "")
        max_trust = 0.0
        for partner in same_place_chars:
            trust = relationships.get((char_id, partner), {}).get("trust", 0.0)
            if trust > max_trust:
                max_trust = trust

        hint_level = self.calc_hint_level(trust_threshold, max_trust)
        if hint_level == 0:
            return None

        return {
            "hint_level": hint_level,
            "hint_text": self.build_hint_text(secret_content, hint_level),
        }

from __future__ import annotations

import json
import logging
from datetime import time as dtime
from typing import Any

logger = logging.getLogger(__name__)


class PlaceManager:
    """場所管理・推奨場所取得・異常検知を担う純粋計算クラス。

    DB・LLM 依存なし。move_engine / scheduler / prompt_builder から参照される。
    """

    def __init__(self, places: list[dict[str, Any]]) -> None:
        self._places: dict[str, dict[str, Any]] = {p["id"]: p for p in places}

    # ------------------------------------------------------------------
    # プライベートヘルパー
    # ------------------------------------------------------------------

    def _get_adjacent_dict(self, place_id: str) -> dict[str, int]:
        """adjacent_places カラムを dict[place_id → cost] として返す。
        JSON 文字列でも dict でも対応。"""
        place = self._places.get(place_id)
        if not place:
            return {}
        adj = place.get("adjacent_places") or {}
        if isinstance(adj, str):
            adj = json.loads(adj)
        return adj

    def _parse_hhmm(self, s: str) -> dtime:
        """'HH:MM' 文字列を datetime.time に変換。"""
        h, m = s.split(":")
        return dtime(int(h), int(m))

    def _is_in_time_range(self, current: str, time_from: str, time_to: str) -> bool:
        """現在時刻 (HH:MM) が [time_from, time_to] に含まれるか判定。
        time_from > time_to の場合（22:00〜07:00 等）は日またぎとして扱う。"""
        cur = self._parse_hhmm(current)
        frm = self._parse_hhmm(time_from)
        to_ = self._parse_hhmm(time_to)
        if frm <= to_:
            return frm <= cur <= to_
        else:  # 跨日
            return cur >= frm or cur <= to_

    # ------------------------------------------------------------------
    # 基本情報取得
    # ------------------------------------------------------------------

    def get_place_label(self, place_id: str) -> str:
        """place_id → 日本語ラベル。未知の場合は place_id をそのまま返す。"""
        place = self._places.get(place_id)
        if not place:
            return place_id
        return place.get("label", place_id)

    def get_place_atmosphere(self, place_id: str) -> str | None:
        """場所の雰囲気テキスト。未知の場合は None。"""
        place = self._places.get(place_id)
        if not place:
            return None
        return place.get("atmosphere")

    def get_place_zone(self, place_id: str) -> str | None:
        """zone（"school" / "town" / "home"）。未知の場合は None。"""
        place = self._places.get(place_id)
        if not place:
            return None
        return place.get("zone")

    def get_who_gathers(self, place_id: str) -> str:
        """who_gathers テキスト（'ルナ、ミリティア' 等）。未知は空文字。"""
        place = self._places.get(place_id)
        if not place:
            return ""
        return place.get("who_gathers") or ""

    # ------------------------------------------------------------------
    # 移動関連
    # ------------------------------------------------------------------

    def is_adjacent(self, place_id: str, other_id: str) -> bool:
        """2つの場所が隣接しているか。adjacent_places の keys に含まれるかで判定。"""
        return other_id in self._get_adjacent_dict(place_id)

    def get_adjacent_places(self, place_id: str) -> list[str]:
        """隣接場所の ID リストを返す。"""
        return list(self._get_adjacent_dict(place_id).keys())

    def get_move_cost(self, from_id: str, to_id: str) -> int | None:
        """from → to の移動コスト。隣接していない場合は None。"""
        adj = self._get_adjacent_dict(from_id)
        return adj.get(to_id)

    # ------------------------------------------------------------------
    # 推奨場所取得
    # ------------------------------------------------------------------

    def get_recommended_place(
        self,
        time_schedules: list[dict[str, Any]],
        sim_datetime: str,  # "YYYY-MM-DDTHH:MM"
        weekday_type: str,  # "weekday" or "weekend"
    ) -> str | None:
        """現在時刻とスケジュールから推奨場所 ID を返す。マッチなしは None。"""
        current_hhmm = sim_datetime[11:16]

        for sched in time_schedules:
            if sched.get("day_type") != weekday_type:
                continue
            if not self._is_in_time_range(
                current_hhmm, sched["time_from"], sched["time_to"]
            ):
                continue
            expected = sched.get("expected_places", [])
            if isinstance(expected, str):
                expected = json.loads(expected)
            if expected:
                return expected[0]

        return None

    # ------------------------------------------------------------------
    # 異常検知
    # ------------------------------------------------------------------

    def detect_anomaly(
        self,
        current_place: str,
        sim_datetime: str,  # "YYYY-MM-DDTHH:MM"
        anomaly_rules: list[dict[str, Any]],
        alone: bool = True,
        same_place_chars: list[str] | None = None,
        char_stress: float | None = None,
    ) -> dict[str, Any] | None:
        """異常ルールと現在状況を照合し、マッチした最初のルールを返す。

        戻り値:
            {
                "label": str,
                "drama_potential": str,
                "suggested_reason": str,  # suggested_reasons[0]
            }
        マッチなしは None。
        """
        current_hhmm = sim_datetime[11:16]

        for rule in anomaly_rules:
            cond_raw = rule.get("condition_json") or {}
            if isinstance(cond_raw, str):
                cond_raw = json.loads(cond_raw)
            cond: dict[str, Any] = cond_raw

            matched = True

            # place 判定
            if "place" in cond:
                if current_place != cond["place"]:
                    matched = False

            # zone 判定
            if matched and "zone" in cond:
                if self.get_place_zone(current_place) != cond["zone"]:
                    matched = False

            # time 範囲判定
            if matched and "time_from" in cond and "time_to" in cond:
                if not self._is_in_time_range(
                    current_hhmm, cond["time_from"], cond["time_to"]
                ):
                    matched = False

            # alone 判定
            if matched and "alone" in cond:
                if cond["alone"] != alone:
                    matched = False

            # multiple_characters 判定
            if matched and "multiple_characters" in cond:
                if cond["multiple_characters"] and len(same_place_chars or []) < 2:
                    matched = False

            # stress_threshold_min 判定
            if matched and "stress_threshold_min" in cond:
                if char_stress is None or char_stress < cond["stress_threshold_min"]:
                    matched = False

            # consecutive_days_min / expected_place はスキップ（現ターン判定不可）

            if matched:
                reasons = rule.get("suggested_reasons", [])
                if isinstance(reasons, str):
                    reasons = json.loads(reasons)
                suggested_reason = reasons[0] if reasons else ""
                return {
                    "label": rule["label"],
                    "drama_potential": rule.get("drama_potential", ""),
                    "suggested_reason": suggested_reason,
                }

        return None

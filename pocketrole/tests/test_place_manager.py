"""Tests for engine/place_manager.py — 14 test cases."""

from __future__ import annotations

import pytest

from engine.place_manager import PlaceManager

# ---------------------------------------------------------------------------
# フィクスチャデータ
# ---------------------------------------------------------------------------

PLACES = [
    {
        "id": "classroom",
        "label": "教室",
        "zone": "school",
        "atmosphere": "日常的",
        "who_gathers": "全員",
        "adjacent_places": {"corridor": 1},
    },
    {
        "id": "rooftop",
        "label": "屋上",
        "zone": "school",
        "atmosphere": "開放的",
        "who_gathers": "ユウマ",
        "adjacent_places": {"corridor": 2},
    },
    {
        "id": "corridor",
        "label": "廊下",
        "zone": "school",
        "atmosphere": "移動の場",
        "who_gathers": "全員",
        "adjacent_places": {"classroom": 1, "rooftop": 2, "music_room": 1},
    },
    {
        "id": "music_room",
        "label": "音楽室",
        "zone": "school",
        "atmosphere": "楽器の香り",
        "who_gathers": "ルナ",
        "adjacent_places": {"corridor": 1},
    },
    {
        "id": "town_park",
        "label": "公園",
        "zone": "town",
        "atmosphere": "開放的な屋外",
        "who_gathers": "ミリティア",
        "adjacent_places": {"school_gate": 2},
    },
]

TIME_SCHEDULES = [
    {
        "day_type": "weekday",
        "time_from": "08:30",
        "time_to": "12:00",
        "label": "午前授業",
        "expected_places": ["classroom"],
    },
    {
        "day_type": "weekday",
        "time_from": "22:00",
        "time_to": "07:00",
        "label": "就寝",
        "expected_places": ["home"],
    },
    {
        "day_type": "weekend",
        "time_from": "09:00",
        "time_to": "12:00",
        "label": "午前（自由）",
        "expected_places": ["home", "town_park"],
    },
]

ANOMALY_RULES = [
    {
        "label": "深夜の音楽室",
        "condition_json": {
            "time_from": "22:00",
            "time_to": "07:00",
            "place": "music_room",
        },
        "drama_potential": "隠れて練習している？",
        "suggested_reasons": ["誰にも聞かれたくない曲を練習している"],
    },
    {
        "label": "授業中の屋上",
        "condition_json": {
            "time_from": "08:30",
            "time_to": "16:30",
            "place": "rooftop",
        },
        "drama_potential": "逃避？",
        "suggested_reasons": ["授業が辛くて逃げ出した"],
    },
    {
        "label": "夜の独り彷徨",
        "condition_json": {
            "time_from": "21:00",
            "time_to": "07:00",
            "zone": "town",
            "alone": True,
        },
        "drama_potential": "秘密の用事？",
        "suggested_reasons": ["考えを整理するために歩いている"],
    },
    {
        "label": "音楽室の衝突",
        "condition_json": {
            "place": "music_room",
            "multiple_characters": True,
            "stress_threshold_min": 0.7,
        },
        "drama_potential": "練習の競争・衝突",
        "suggested_reasons": ["練習スペースの取り合い"],
    },
]


@pytest.fixture
def pm() -> PlaceManager:
    return PlaceManager(PLACES)


# ---------------------------------------------------------------------------
# テストケース 1〜2: get_place_label
# ---------------------------------------------------------------------------


def test_get_place_label_found(pm: PlaceManager) -> None:
    """既知の place_id は日本語ラベルを返す。"""
    assert pm.get_place_label("classroom") == "教室"


def test_get_place_label_unknown(pm: PlaceManager) -> None:
    """未知の place_id は place_id をそのまま返す。"""
    assert pm.get_place_label("unknown_place") == "unknown_place"


# ---------------------------------------------------------------------------
# テストケース 3〜4: is_adjacent
# ---------------------------------------------------------------------------


def test_is_adjacent_true(pm: PlaceManager) -> None:
    """classroom ↔ corridor は隣接している。"""
    assert pm.is_adjacent("classroom", "corridor") is True


def test_is_adjacent_false(pm: PlaceManager) -> None:
    """classroom ↔ rooftop は隣接していない。"""
    assert pm.is_adjacent("classroom", "rooftop") is False


# ---------------------------------------------------------------------------
# テストケース 5: get_adjacent_places
# ---------------------------------------------------------------------------


def test_get_adjacent_places(pm: PlaceManager) -> None:
    """corridor の隣接リストが正しく返る。"""
    adj = pm.get_adjacent_places("corridor")
    assert set(adj) == {"classroom", "rooftop", "music_room"}


# ---------------------------------------------------------------------------
# テストケース 6〜7: get_move_cost
# ---------------------------------------------------------------------------


def test_get_move_cost_adjacent(pm: PlaceManager) -> None:
    """classroom → corridor の移動コストは 1。"""
    assert pm.get_move_cost("classroom", "corridor") == 1


def test_get_move_cost_not_adjacent(pm: PlaceManager) -> None:
    """classroom → rooftop は隣接していないので None。"""
    assert pm.get_move_cost("classroom", "rooftop") is None


# ---------------------------------------------------------------------------
# テストケース 8: get_place_atmosphere
# ---------------------------------------------------------------------------


def test_get_place_atmosphere(pm: PlaceManager) -> None:
    """classroom の atmosphere が返る。"""
    assert pm.get_place_atmosphere("classroom") == "日常的"


# ---------------------------------------------------------------------------
# テストケース 9: get_place_zone
# ---------------------------------------------------------------------------


def test_get_place_zone(pm: PlaceManager) -> None:
    """town_park の zone は "town"。"""
    assert pm.get_place_zone("town_park") == "town"


# ---------------------------------------------------------------------------
# テストケース 10〜12: get_recommended_place
# ---------------------------------------------------------------------------


def test_get_recommended_place_match(pm: PlaceManager) -> None:
    """平日 09:00 は 08:30〜12:00 にマッチ → 'classroom' を返す。"""
    result = pm.get_recommended_place(
        TIME_SCHEDULES,
        sim_datetime="2025-04-07T09:00",
        weekday_type="weekday",
    )
    assert result == "classroom"


def test_get_recommended_place_overnight(pm: PlaceManager) -> None:
    """平日 23:30 は 22:00〜07:00（跨日）にマッチ → 'home' を返す。"""
    result = pm.get_recommended_place(
        TIME_SCHEDULES,
        sim_datetime="2025-04-07T23:30",
        weekday_type="weekday",
    )
    assert result == "home"


def test_get_recommended_place_no_match(pm: PlaceManager) -> None:
    """平日 07:30 はどの時間帯にも該当しない → None。"""
    result = pm.get_recommended_place(
        TIME_SCHEDULES,
        sim_datetime="2025-04-07T07:30",
        weekday_type="weekday",
    )
    assert result is None


# ---------------------------------------------------------------------------
# テストケース 13〜14: detect_anomaly
# ---------------------------------------------------------------------------


def test_detect_anomaly_match_time_place(pm: PlaceManager) -> None:
    """平日 23:00 に music_room → '深夜の音楽室' にマッチ。"""
    result = pm.detect_anomaly(
        current_place="music_room",
        sim_datetime="2025-04-07T23:00",
        anomaly_rules=ANOMALY_RULES,
        alone=True,
    )
    assert result is not None
    assert result["label"] == "深夜の音楽室"
    assert result["drama_potential"] == "隠れて練習している？"
    assert result["suggested_reason"] == "誰にも聞かれたくない曲を練習している"


def test_detect_anomaly_no_match(pm: PlaceManager) -> None:
    """平日 09:00 に classroom → どのルールにも該当しない → None。"""
    result = pm.detect_anomaly(
        current_place="classroom",
        sim_datetime="2025-04-07T09:00",
        anomaly_rules=ANOMALY_RULES,
        alone=False,
    )
    assert result is None

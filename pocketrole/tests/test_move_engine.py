from __future__ import annotations

import random

import pytest

from engine.move_engine import MoveEngine
from engine.place_manager import PlaceManager

# ---------------------------------------------------------------------------
# テストフィクスチャ
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
        "id": "isolated_room",
        "label": "隔離部屋",
        "zone": "school",
        "atmosphere": "孤独",
        "who_gathers": "",
        "adjacent_places": {},
    },
]

NEUTRAL_EMOTIONS = {
    "stress": 0.5,
    "motivation": 0.5,
    "loneliness": 0.5,
    "excitement": 0.5,
}


@pytest.fixture
def pm() -> PlaceManager:
    return PlaceManager(PLACES)


# ---------------------------------------------------------------------------
# テスト: calc_move_probability
# ---------------------------------------------------------------------------


def test_calc_move_prob_baseline(pm: PlaceManager) -> None:
    """中立感情・推奨場所なし → base_move_prob(0.3)"""
    engine = MoveEngine(pm, base_move_prob=0.3)
    prob = engine.calc_move_probability(NEUTRAL_EMOTIONS, "classroom", None)
    assert prob == pytest.approx(0.3)


def test_calc_move_prob_high_motivation_up(pm: PlaceManager) -> None:
    """motivation=1.0（+0.5 偏差 × 0.4 = +0.2） → 0.3 + 0.2 = 0.5"""
    engine = MoveEngine(pm, base_move_prob=0.3)
    emotions = {**NEUTRAL_EMOTIONS, "motivation": 1.0}
    prob = engine.calc_move_probability(emotions, "classroom", None)
    assert prob == pytest.approx(0.5)


def test_calc_move_prob_high_stress_up(pm: PlaceManager) -> None:
    """stress=1.0（+0.5 偏差 × 0.2 = +0.1） → 0.3 + 0.1 = 0.4"""
    engine = MoveEngine(pm, base_move_prob=0.3)
    emotions = {**NEUTRAL_EMOTIONS, "stress": 1.0}
    prob = engine.calc_move_probability(emotions, "classroom", None)
    assert prob == pytest.approx(0.4)


def test_calc_move_prob_schedule_mismatch_up(pm: PlaceManager) -> None:
    """推奨場所≠現在地 → 0.3 + 0.3 = 0.6（中立感情）"""
    engine = MoveEngine(pm, base_move_prob=0.3)
    prob = engine.calc_move_probability(NEUTRAL_EMOTIONS, "classroom", "corridor")
    assert prob == pytest.approx(0.6)


def test_calc_move_prob_schedule_match_down(pm: PlaceManager) -> None:
    """推奨場所==現在地 → 0.3 - 0.2 = 0.1（中立感情）"""
    engine = MoveEngine(pm, base_move_prob=0.3)
    prob = engine.calc_move_probability(NEUTRAL_EMOTIONS, "classroom", "classroom")
    assert prob == pytest.approx(0.1)


def test_calc_move_prob_group_stay_bias_down(pm: PlaceManager) -> None:
    """同席相手がいるときは離脱しにくくなる。"""
    engine = MoveEngine(pm, base_move_prob=0.3)
    prob = engine.calc_move_probability(
        NEUTRAL_EMOTIONS,
        "classroom",
        "classroom",
        same_place_count=1,
    )
    assert prob == pytest.approx(0.0)


def test_calc_move_prob_social_pull_up(pm: PlaceManager) -> None:
    """単独かつ social target があると move probability が上がる。"""
    engine = MoveEngine(pm, base_move_prob=0.3)
    emotions = {**NEUTRAL_EMOTIONS, "loneliness": 0.7}
    prob = engine.calc_move_probability(
        emotions,
        "classroom",
        None,
        same_place_count=0,
        social_target_place="corridor",
        social_pull_strength=1.0,
    )
    assert prob == pytest.approx(0.54)


def test_calc_move_prob_current_place_pull_down(pm: PlaceManager) -> None:
    """現在地に scene opportunity があると離脱しにくくなる。"""
    engine = MoveEngine(pm, base_move_prob=0.3)
    prob = engine.calc_move_probability(
        NEUTRAL_EMOTIONS,
        "classroom",
        None,
        current_place_pull_strength=1.0,
    )
    assert prob == pytest.approx(0.1)


def test_calc_move_prob_clamped(pm: PlaceManager) -> None:
    """極端な感情値でも 0.0〜1.0 にクランプされる"""
    engine = MoveEngine(pm, base_move_prob=0.3)

    # 上限: motivation=1.0 + stress=1.0 + loneliness=1.0 + schedule mismatch → 超過
    emotions_high = {"stress": 1.0, "motivation": 1.0, "loneliness": 1.0, "excitement": 1.0}
    prob_high = engine.calc_move_probability(emotions_high, "classroom", "corridor")
    assert prob_high == pytest.approx(1.0)

    # 下限: all=0.0 + schedule match → 下回る可能性
    emotions_low = {"stress": 0.0, "motivation": 0.0, "loneliness": 0.0, "excitement": 0.0}
    prob_low = engine.calc_move_probability(emotions_low, "classroom", "classroom")
    assert prob_low == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# テスト: should_move
# ---------------------------------------------------------------------------


def test_should_move_returns_true(pm: PlaceManager) -> None:
    """base_move_prob=1.0 → 常に True"""
    engine = MoveEngine(pm, base_move_prob=1.0, rng=random.Random(42))
    assert engine.should_move(NEUTRAL_EMOTIONS, "classroom", None) is True


def test_should_move_returns_false(pm: PlaceManager) -> None:
    """base_move_prob=0.0、中立感情 → 常に False"""
    engine = MoveEngine(pm, base_move_prob=0.0, rng=random.Random(42))
    assert engine.should_move(NEUTRAL_EMOTIONS, "classroom", None) is False


# ---------------------------------------------------------------------------
# テスト: decide_destination
# ---------------------------------------------------------------------------


def test_decide_destination_recommended_adjacent(pm: PlaceManager) -> None:
    """recommended=corridor（classroom の隣）→ corridor を返す"""
    engine = MoveEngine(pm, rng=random.Random(0))
    dest = engine.decide_destination("classroom", "corridor")
    assert dest == "corridor"


def test_decide_destination_recommended_not_adjacent(pm: PlaceManager) -> None:
    """recommended=rooftop（classroom から非隣接）→ 隣接する corridor を返す"""
    engine = MoveEngine(pm, rng=random.Random(0))
    dest = engine.decide_destination("classroom", "rooftop")
    # classroom の隣接は corridor のみ
    assert dest == "corridor"


def test_decide_destination_no_adjacent(pm: PlaceManager) -> None:
    """current=isolated_room（隣接なし）→ isolated_room のまま"""
    engine = MoveEngine(pm, rng=random.Random(0))
    dest = engine.decide_destination("isolated_room", None)
    assert dest == "isolated_room"


def test_decide_destination_no_recommended(pm: PlaceManager) -> None:
    """recommended=None、current=music_room → 唯一の隣接 corridor を返す"""
    engine = MoveEngine(pm, rng=random.Random(0))
    dest = engine.decide_destination("music_room", None)
    # music_room の隣接は corridor のみ
    assert dest == "corridor"


def test_decide_destination_prefers_social_target(pm: PlaceManager) -> None:
    """social target が隣接していれば最優先する。"""
    engine = MoveEngine(pm, rng=random.Random(0))
    dest = engine.decide_destination(
        "corridor",
        "classroom",
        social_target_place="music_room",
    )
    assert dest == "music_room"


# ---------------------------------------------------------------------------
# テスト: generate_move_reason
# ---------------------------------------------------------------------------


def test_generate_move_reason_schedule(pm: PlaceManager) -> None:
    """recommended==destination="corridor" → "スケジュール" を含む文字列"""
    engine = MoveEngine(pm)
    reason = engine.generate_move_reason("classroom", "corridor", NEUTRAL_EMOTIONS, "corridor")
    assert "スケジュール" in reason


def test_generate_move_reason_high_stress(pm: PlaceManager) -> None:
    """stress=0.9 → "気分転換" を含む文字列"""
    engine = MoveEngine(pm)
    emotions = {**NEUTRAL_EMOTIONS, "stress": 0.9}
    reason = engine.generate_move_reason("classroom", "corridor", emotions, None)
    assert "気分転換" in reason

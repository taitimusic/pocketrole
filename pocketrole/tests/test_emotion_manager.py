"""tests/test_emotion_manager.py — EmotionManager の単体テスト（14件）"""
from __future__ import annotations

import logging

import pytest

from engine.emotion_manager import EmotionManager, EMOTION_TRIGGERS, EMOTION_KEYS


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------

def make_neutral_emotions() -> dict[str, float]:
    return {"stress": 0.5, "motivation": 0.5, "loneliness": 0.5, "excitement": 0.5}


def make_emotions(**overrides: float) -> dict[str, float]:
    e = make_neutral_emotions()
    e.update(overrides)
    return e


# ---------------------------------------------------------------------------
# fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def em() -> EmotionManager:
    return EmotionManager()


# ---------------------------------------------------------------------------
# clamp
# ---------------------------------------------------------------------------

def test_clamp_within_range(em: EmotionManager) -> None:
    """0.0〜1.0 の値はそのまま返る。"""
    assert em.clamp(0.0) == 0.0
    assert em.clamp(0.5) == 0.5
    assert em.clamp(1.0) == 1.0


def test_clamp_above_max(em: EmotionManager) -> None:
    """1.0 超は 1.0 にクランプされる。"""
    assert em.clamp(1.5) == 1.0
    assert em.clamp(2.0) == 1.0


def test_clamp_below_min(em: EmotionManager) -> None:
    """0.0 未満は 0.0 にクランプされる。"""
    assert em.clamp(-0.2) == 0.0
    assert em.clamp(-1.0) == 0.0


def test_clamp_emotions_all_keys(em: EmotionManager) -> None:
    """clamp_emotions は全 4 キーをクランプした新しい dict を返す。"""
    raw = {"stress": 1.5, "motivation": -0.1, "loneliness": 0.5, "excitement": 2.0}
    result = em.clamp_emotions(raw)
    assert result["stress"] == 1.0
    assert result["motivation"] == 0.0
    assert result["loneliness"] == 0.5
    assert result["excitement"] == 1.0
    # 元の dict は破壊しない
    assert raw["stress"] == 1.5


# ---------------------------------------------------------------------------
# apply_trigger
# ---------------------------------------------------------------------------

def test_apply_trigger_moved(em: EmotionManager) -> None:
    """'moved' トリガーで stress -0.1, motivation +0.05 が適用される。"""
    emotions = make_emotions(stress=0.5, motivation=0.5)
    result = em.apply_trigger(emotions, "moved")
    assert abs(result["stress"] - 0.4) < 1e-9
    assert abs(result["motivation"] - 0.55) < 1e-9
    # loneliness は変化しない
    assert result["loneliness"] == 0.5


def test_apply_trigger_alone_long(em: EmotionManager) -> None:
    """'alone_long' トリガーで loneliness +0.15, motivation -0.1 が適用される。"""
    emotions = make_emotions(loneliness=0.5, motivation=0.5)
    result = em.apply_trigger(emotions, "alone_long")
    assert abs(result["loneliness"] - 0.65) < 1e-9
    assert abs(result["motivation"] - 0.4) < 1e-9


def test_apply_trigger_unknown(
    em: EmotionManager, caplog: pytest.LogCaptureFixture
) -> None:
    """未知の trigger_type は warning ログを出しデルタなしで返す。"""
    emotions = make_neutral_emotions()
    with caplog.at_level(logging.WARNING, logger="engine.emotion_manager"):
        result = em.apply_trigger(emotions, "nonexistent_trigger")
    assert "nonexistent_trigger" in caplog.text
    # 感情値は変化しない
    assert result == em.clamp_emotions(emotions)


def test_apply_trigger_extra_delta(em: EmotionManager) -> None:
    """extra_delta が正しく追加適用される。"""
    emotions = make_emotions(motivation=0.5)
    result = em.apply_trigger(emotions, "memory_recall", extra_delta={"motivation": 0.05})
    # memory_recall: motivation +0.1, extra: motivation +0.05 → 0.65
    assert abs(result["motivation"] - 0.65) < 1e-9


def test_apply_trigger_clamps(em: EmotionManager) -> None:
    """加算後に 1.0 を超えても 1.0 にクランプされる。"""
    emotions = make_emotions(excitement=0.9)
    result = em.apply_trigger(emotions, "event_day")  # excitement +0.3 → 1.2 → 1.0
    assert result["excitement"] == 1.0


# ---------------------------------------------------------------------------
# apply_decay
# ---------------------------------------------------------------------------

def test_apply_decay_toward_neutral(em: EmotionManager) -> None:
    """中立から離れた値が 0.5 方向に近づく。"""
    emotions = make_emotions(stress=0.8, motivation=0.2)
    result = em.apply_decay(emotions, decay_rate=0.05)
    # stress: 0.8 + (0.5 - 0.8) * 0.05 = 0.8 - 0.015 = 0.785
    assert abs(result["stress"] - 0.785) < 1e-9
    # motivation: 0.2 + (0.5 - 0.2) * 0.05 = 0.2 + 0.015 = 0.215
    assert abs(result["motivation"] - 0.215) < 1e-9


def test_apply_decay_already_neutral(em: EmotionManager) -> None:
    """既に 0.5 の値は減衰後も 0.5 のまま。"""
    emotions = make_neutral_emotions()
    result = em.apply_decay(emotions, decay_rate=0.05)
    for key in EMOTION_KEYS:
        assert abs(result[key] - 0.5) < 1e-9


def test_apply_decay_custom_rate(em: EmotionManager) -> None:
    """decay_rate=0.1 で変化量が正確に計算される。"""
    emotions = make_emotions(loneliness=0.8)
    result = em.apply_decay(emotions, decay_rate=0.1)
    # 0.8 + (0.5 - 0.8) * 0.1 = 0.8 - 0.03 = 0.77
    assert abs(result["loneliness"] - 0.77) < 1e-9


# ---------------------------------------------------------------------------
# judge_expression
# ---------------------------------------------------------------------------

def test_judge_expression_happy(em: EmotionManager) -> None:
    """motivation=0.8, excitement=0.7 → 'happy'。"""
    emotions = make_emotions(motivation=0.8, excitement=0.7)
    assert em.judge_expression(emotions) == "happy"


def test_judge_expression_priority(em: EmotionManager) -> None:
    """stress=0.8 (>0.7) の時は 'angry'（happy より優先度が低いため、
    happy 条件を満たさない値で確認）。"""
    # stress=0.8, motivation=0.4, excitement=0.4 → happy 条件不成立 → angry
    emotions = make_emotions(stress=0.8, motivation=0.4, excitement=0.4)
    assert em.judge_expression(emotions) == "angry"


def test_judge_expression_neutral(em: EmotionManager) -> None:
    """全て 0.5 → 'neutral'。"""
    emotions = make_neutral_emotions()
    assert em.judge_expression(emotions) == "neutral"

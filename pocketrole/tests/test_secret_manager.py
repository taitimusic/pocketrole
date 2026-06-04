"""tests/test_secret_manager.py — SecretManager クラスのテスト（14件）"""
from __future__ import annotations

import pytest
from engine.secret_manager import SecretManager

# --- フィクスチャ・共通データ ---

@pytest.fixture
def sm() -> SecretManager:
    return SecretManager()


CHAR_WITH_SECRET = {
    "id": "yuuma",
    "secret": "音楽に強いコンプレックスを持つ。歌えない・楽器も弾けない。それを必死に隠している",
    "secret_reveal_condition": "信頼度0.8以上のキャラクターと1対1で密室（屋上・音楽室）で会う",
}

CHAR_NO_SECRET_NONE = {
    "id": "runa",
    "secret": None,
    "secret_reveal_condition": None,
}

CHAR_NO_SECRET_EMPTY = {
    "id": "aria",
    "secret": "",
    "secret_reveal_condition": "信頼度0.7以上かつ音楽室で2人きりになる",
}

CHAR_NO_THRESHOLD = {
    "id": "miria",
    "secret": "傷ついた過去がある",
    "secret_reveal_condition": "条件が整ったら自発的に打ち明ける",  # 信頼度表記なし
}

RELATIONSHIPS: dict[tuple[str, str], dict] = {
    ("yuuma", "luna"):  {"trust": 0.85},   # 閾値(0.8)を超える → level 3
    ("yuuma", "aria"):  {"trust": 0.60},   # 閾値の75% → level 1
    ("yuuma", "souma"): {"trust": 0.73},   # 閾値の91% → level 2
}


# --- check_reveal: 秘密なしケース ---

def test_no_secret_none_returns_none(sm: SecretManager) -> None:
    """char.secret=None → check_reveal=None"""
    result = sm.check_reveal(CHAR_NO_SECRET_NONE, ["luna"], RELATIONSHIPS)
    assert result is None


def test_no_secret_empty_returns_none(sm: SecretManager) -> None:
    """char.secret='' → check_reveal=None"""
    result = sm.check_reveal(CHAR_NO_SECRET_EMPTY, ["luna"], RELATIONSHIPS)
    assert result is None


# --- _extract_trust_threshold ---

def test_extract_threshold_standard(sm: SecretManager) -> None:
    """'信頼度0.8以上...' → 0.8"""
    result = sm._extract_trust_threshold("信頼度0.8以上のキャラクターと1対1で会う")
    assert result == pytest.approx(0.8)


def test_extract_threshold_two_decimal(sm: SecretManager) -> None:
    """'信頼度0.75以上...' → 0.75"""
    result = sm._extract_trust_threshold("信頼度0.75以上かつ猫耳について直接聞かれる")
    assert result == pytest.approx(0.75)


def test_extract_threshold_no_match(sm: SecretManager) -> None:
    """信頼度パターンなし → None"""
    result = sm._extract_trust_threshold("条件が整ったら自発的に打ち明ける")
    assert result is None


# --- check_reveal: 閾値抽出不可 ---

def test_no_threshold_returns_none(sm: SecretManager) -> None:
    """条件テキストに信頼度数値なし → check_reveal=None"""
    result = sm.check_reveal(CHAR_NO_THRESHOLD, ["luna"], RELATIONSHIPS)
    assert result is None


# --- calc_hint_level ---

def test_calc_hint_level_zero(sm: SecretManager) -> None:
    """max_trust=0.4, threshold=0.8 → 0（40% < 70%）"""
    assert sm.calc_hint_level(0.8, 0.4) == 0


def test_calc_hint_level_stage1(sm: SecretManager) -> None:
    """max_trust=0.60, threshold=0.8 → 1（75% ≥ 70%）"""
    assert sm.calc_hint_level(0.8, 0.60) == 1


def test_calc_hint_level_stage2(sm: SecretManager) -> None:
    """max_trust=0.73, threshold=0.8 → 2（91.25% ≥ 90%）"""
    assert sm.calc_hint_level(0.8, 0.73) == 2


def test_calc_hint_level_full_reveal(sm: SecretManager) -> None:
    """max_trust=0.8, threshold=0.8 → 3（100%以上）"""
    assert sm.calc_hint_level(0.8, 0.8) == 3


# --- build_hint_text ---

def test_build_hint_text_level0_is_none(sm: SecretManager) -> None:
    """level=0 → None"""
    assert sm.build_hint_text("秘密内容", 0) is None


def test_build_hint_text_level1_nonempty(sm: SecretManager) -> None:
    """level=1 → 非空 str、秘密内容を含まない"""
    secret = "音楽コンプレックス"
    result = sm.build_hint_text(secret, 1)
    assert isinstance(result, str)
    assert len(result) > 0
    assert secret not in result


def test_build_hint_text_level3_contains_secret(sm: SecretManager) -> None:
    """level=3 → str が secret_content を含む"""
    secret = "音楽に強いコンプレックスを持つ"
    result = sm.build_hint_text(secret, 3)
    assert isinstance(result, str)
    assert secret in result


# --- check_reveal: エンドツーエンド ---

def test_check_reveal_end_to_end(sm: SecretManager) -> None:
    """完全シナリオ: luna(trust=0.85) が同じ場所 → hint_level=3, hint_text に秘密内容を含む"""
    result = sm.check_reveal(CHAR_WITH_SECRET, ["luna"], RELATIONSHIPS)
    assert result is not None
    assert result["hint_level"] == 3
    assert CHAR_WITH_SECRET["secret"] in result["hint_text"]

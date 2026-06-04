"""tests/test_scheduler.py — Scheduler クラスのテスト（14件）"""
from __future__ import annotations

import pytest
from engine.scheduler import Scheduler

CHAR_ID = "yuuma"
PARTNER_A = "luna"
PARTNER_B = "aria"
PARTNER_C = "miria"

# (from, to) → {"trust": float} 形式
RELATIONSHIPS: dict[tuple[str, str], dict] = {
    ("yuuma", "luna"):  {"trust": 0.8},
    ("yuuma", "aria"):  {"trust": 0.4},
    ("yuuma", "miria"): {"trust": 0.7},
}


@pytest.fixture
def sched() -> Scheduler:
    return Scheduler()  # default threshold=0.6


# --- monologue ケース ---

def test_schedule_monologue_alone(sched: Scheduler) -> None:
    """same_place_chars=[] → monologue、target=None"""
    result = sched.schedule_utterance(CHAR_ID, [], RELATIONSHIPS)
    assert result["msg_type"] == "monologue"
    assert result["target_char_id"] is None


def test_schedule_monologue_low_trust(sched: Scheduler) -> None:
    """1人(aria, trust=0.4) でも同席相手がいれば reply を優先する。"""
    result = sched.schedule_utterance(CHAR_ID, [PARTNER_B], RELATIONSHIPS)
    assert result["msg_type"] == "reply"
    assert result["target_char_id"] == PARTNER_B


def test_schedule_monologue_no_relationship_entry(sched: Scheduler) -> None:
    """関係エントリなしでも、1対1なら reply を優先する。"""
    result = sched.schedule_utterance(CHAR_ID, ["unknown_char"], RELATIONSHIPS)
    assert result["msg_type"] == "reply"
    assert result["target_char_id"] == "unknown_char"


def test_schedule_monologue_at_threshold(sched: Scheduler) -> None:
    """信頼度が境界値でも、1対1なら reply を優先する。"""
    rels: dict[tuple[str, str], dict] = {("yuuma", "exact"): {"trust": 0.6}}
    result = sched.schedule_utterance(CHAR_ID, ["exact"], rels)
    assert result["msg_type"] == "reply"
    assert result["target_char_id"] == "exact"


# --- reply ケース ---

def test_schedule_reply_high_trust(sched: Scheduler) -> None:
    """1人(luna, trust=0.8) → reply"""
    result = sched.schedule_utterance(CHAR_ID, [PARTNER_A], RELATIONSHIPS)
    assert result["msg_type"] == "reply"
    assert result["target_char_id"] == PARTNER_A


def test_schedule_reply_when_recent_dialogue_exists_even_if_low_trust(sched: Scheduler) -> None:
    """recent dialogue がある 1対1 では low trust でも reply を優先する。"""
    result = sched.schedule_utterance(
        CHAR_ID,
        [PARTNER_B],
        RELATIONSHIPS,
        has_recent_dialogue=True,
    )
    assert result["msg_type"] == "reply"
    assert result["target_char_id"] == PARTNER_B


def test_schedule_reply_when_speaker_intent_is_react(sched: Scheduler) -> None:
    """scene react speaker は low trust でも reply を優先する。"""
    result = sched.schedule_utterance(
        CHAR_ID,
        [PARTNER_B],
        RELATIONSHIPS,
        speaker_intent="react",
    )
    assert result["msg_type"] == "reply"
    assert result["target_char_id"] == PARTNER_B


def test_reply_target_is_string(sched: Scheduler) -> None:
    """reply の target_char_id は str 型（list でない）"""
    result = sched.schedule_utterance(CHAR_ID, [PARTNER_A], RELATIONSHIPS)
    assert result["msg_type"] == "reply"
    assert isinstance(result["target_char_id"], str)


# --- group ケース ---

def test_schedule_group_two_others(sched: Scheduler) -> None:
    """2人(luna, aria) → group"""
    result = sched.schedule_utterance(CHAR_ID, [PARTNER_A, PARTNER_B], RELATIONSHIPS)
    assert result["msg_type"] == "group"


def test_schedule_reply_in_group_when_recent_dialogue_exists(sched: Scheduler) -> None:
    """multi-party でも recent dialogue があれば opener ではなく reply を返す。"""
    result = sched.schedule_utterance(
        CHAR_ID,
        [PARTNER_A, PARTNER_B],
        RELATIONSHIPS,
        has_recent_dialogue=True,
    )
    assert result["msg_type"] == "reply"
    assert result["target_char_id"] == PARTNER_A


def test_schedule_group_many_others(sched: Scheduler) -> None:
    """3人(luna, aria, miria) → group、target に全員含む"""
    others = [PARTNER_A, PARTNER_B, PARTNER_C]
    result = sched.schedule_utterance(CHAR_ID, others, RELATIONSHIPS)
    assert result["msg_type"] == "group"
    assert set(result["target_char_id"]) == set(others)


def test_group_target_is_list(sched: Scheduler) -> None:
    """group の target_char_id は list 型"""
    result = sched.schedule_utterance(CHAR_ID, [PARTNER_A, PARTNER_B], RELATIONSHIPS)
    assert result["msg_type"] == "group"
    assert isinstance(result["target_char_id"], list)


def test_group_target_excludes_self(sched: Scheduler) -> None:
    """target_char_id に char_id 自身(yuuma)が含まれない"""
    others = [PARTNER_A, PARTNER_B]
    result = sched.schedule_utterance(CHAR_ID, others, RELATIONSHIPS)
    assert CHAR_ID not in result["target_char_id"]


# --- カスタム閾値 ---

def test_custom_threshold_allows_reply() -> None:
    """threshold=0.3 に下げると trust=0.4(aria) → reply"""
    sched = Scheduler(reply_trust_threshold=0.3)
    result = sched.schedule_utterance(CHAR_ID, [PARTNER_B], RELATIONSHIPS)
    assert result["msg_type"] == "reply"
    assert result["target_char_id"] == PARTNER_B


def test_custom_threshold_blocks_reply() -> None:
    """threshold を上げても 1対1の reply 優先は変わらない。"""
    sched = Scheduler(reply_trust_threshold=0.9)
    result = sched.schedule_utterance(CHAR_ID, [PARTNER_A], RELATIONSHIPS)
    assert result["msg_type"] == "reply"
    assert result["target_char_id"] == PARTNER_A


# --- anomaly ---

def test_anomaly_does_not_affect_result(sched: Scheduler) -> None:
    """anomaly={...} を渡しても msg_type の決定に影響しない"""
    anomaly = {"label": "テスト異常", "drama_potential": "high", "suggested_reason": "test"}
    # alone → monologue（anomaly があっても変わらない）
    result_without = sched.schedule_utterance(CHAR_ID, [], RELATIONSHIPS)
    result_with = sched.schedule_utterance(CHAR_ID, [], RELATIONSHIPS, anomaly=anomaly)
    assert result_with["msg_type"] == result_without["msg_type"]
    assert result_with["target_char_id"] == result_without["target_char_id"]


# --- reason ---

def test_reason_is_nonempty_string(sched: Scheduler) -> None:
    """monologue / reply / group すべてで reason が非空 str"""
    cases = [
        sched.schedule_utterance(CHAR_ID, [], RELATIONSHIPS),                       # monologue
        sched.schedule_utterance(CHAR_ID, [PARTNER_A], RELATIONSHIPS),              # reply
        sched.schedule_utterance(CHAR_ID, [PARTNER_A, PARTNER_B], RELATIONSHIPS),  # group
    ]
    for result in cases:
        assert isinstance(result["reason"], str)
        assert len(result["reason"]) > 0

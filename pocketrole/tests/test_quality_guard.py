"""tests/test_quality_guard.py — QualityGuard のテスト。"""

import pytest

from engine.config import QualityGuardConfig
from engine.quality_guard import QualityGuard
from engine.story_style import get_story_style_profile


def _make_guard(**overrides: object) -> QualityGuard:
    config = QualityGuardConfig(
        enabled=True,
        max_group_chars=180,
        max_reply_chars=140,
        max_monologue_chars=110,
        max_abstract_token_occurrences=3,
        max_repeated_ngram_occurrences=2,
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return QualityGuard(config)


def test_quality_guard_strips_markdown_emphasis() -> None:
    guard = _make_guard()

    result = guard.evaluate("**本音**をそのまま話すよ。", msg_type="reply")

    assert result.action == "normalize"
    assert result.text == "本音をそのまま話すよ。"
    assert any(issue["issue_type"] == "markdown_formatting" for issue in result.issues)


def test_quality_guard_shortens_overlength_message() -> None:
    guard = _make_guard(max_reply_chars=12)

    result = guard.evaluate("これはとても長い返答で、制限を超えています。", msg_type="reply")

    assert result.action == "shorten"
    assert len(result.text) <= 12
    assert any(issue["issue_type"] == "overlength" for issue in result.issues)


def test_quality_guard_ignores_current_affairs_url_for_length_checks() -> None:
    guard = _make_guard(max_current_affairs_chars=12)
    long_url = "https://example.com/news/" + ("a" * 120)
    text = f"短い感想。\n{long_url}"

    hard = guard.evaluate_hard(text, msg_type="current_affairs", sanitized_text=text)
    soft = guard.evaluate(text, msg_type="current_affairs")

    assert hard.action == "accept"
    assert soft.action == "accept"
    assert soft.text == text


def test_quality_guard_hard_check_rejects_only_structural_failures() -> None:
    guard = _make_guard(max_reply_chars=16)

    result = guard.evaluate_hard(
        "```json\n{\"text\":\"返答\"}\n```",
        msg_type="reply",
        sanitized_text='{"text":"返答"}',
    )

    assert result.action == "retry"
    issue_types = {issue["issue_type"] for issue in result.issues}
    assert "json_or_code_output" in issue_types
    assert "markdown_formatting" in issue_types


def test_quality_guard_hard_check_allows_semantic_roughness() -> None:
    guard = _make_guard(max_abstract_token_occurrences=2)

    result = guard.evaluate_hard(
        "誰かのために誰かが誰かへ誰かを語る。",
        msg_type="monologue",
        sanitized_text="誰かのために誰かが誰かへ誰かを語る。",
        recent_dialogue_lines=["相手: 「まだ話している」"],
    )

    assert result.action == "accept"
    assert result.issues == []


def test_quality_guard_hard_check_treats_opening_exclamation_as_sentence_prefix() -> None:
    """短い冒頭感嘆を独立文に数えず、実質2文なら保存可能にする。"""
    guard = _make_guard(max_reply_chars=320)
    text = "えぇっ！？何それ、ただのネタでしょ！？あたしのギターの腕前なんて、こんな茶番で評価されるわけないじゃん！！"

    result = guard.evaluate_hard(text, msg_type="reply", sanitized_text=text)

    assert result.action == "accept"
    assert not any(issue["issue_type"] == "sentence_overflow" for issue in result.issues)


def test_quality_guard_hard_check_still_rejects_true_three_sentence_reply() -> None:
    guard = _make_guard(max_reply_chars=320)
    text = "それは違う。順番は先に決める。今ここで役割も分ける。"

    result = guard.evaluate_hard(text, msg_type="reply", sanitized_text=text)

    assert result.action == "retry"
    assert any(issue["issue_type"] == "sentence_overflow" for issue in result.issues)


def test_quality_guard_drops_abstract_repetition_loop() -> None:
    guard = _make_guard(max_abstract_token_occurrences=2)

    result = guard.evaluate("誰かのために誰かが誰かへ誰かを語る。", msg_type="group")

    assert result.action == "drop"
    assert any(issue["issue_type"] == "abstract_repetition" for issue in result.issues)


def test_quality_guard_requests_retry_for_poetic_abstraction_in_ankoku_profile() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "暗闇の運命が静かに揺れ、本音だけが空っぽにこぼれていく。",
        msg_type="monologue",
        style_profile=get_story_style_profile("ankoku_gakuen"),
    )

    assert result.action == "retry"
    assert any(issue["issue_type"] == "poetic_abstraction" for issue in result.issues)


def test_quality_guard_requests_retry_for_poetic_abstraction_globally() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "暗闇の運命が静かに揺れ、本音だけが空っぽにこぼれていく。",
        msg_type="monologue",
    )

    assert result.action == "retry"
    assert any(issue["issue_type"] == "poetic_abstraction" for issue in result.issues)


def test_quality_guard_requests_retry_for_reply_without_direct_reaction() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "今日は空気が重い。たぶん音が逃げる。",
        msg_type="reply",
        target_char_name="星風 瑠奈",
        recent_dialogue_lines=["星風 瑠奈: 「まだ帰らないの？」"],
    )

    assert result.action == "retry"
    assert any(issue["issue_type"] == "reply_without_direct_reaction" for issue in result.issues)


def test_quality_guard_requests_retry_for_director_cue_surface_leak() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "上泉ソーマ、その流れは雑だ。残った所を言葉にする",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「手帳を見せろ」"],
    )

    assert result.action == "retry"
    assert any(issue["issue_type"] == "director_cue_surface_leak" for issue in result.issues)


def test_quality_guard_requests_retry_for_fixed_unfinished_tail() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "夜風ユウマ、それで押し切れると思った？。まだ終わりにしない",
        msg_type="reply",
        target_char_name="夜風ユウマ",
        recent_dialogue_lines=["夜風ユウマ: 「ここで切る」"],
    )

    assert result.action == "retry"
    assert any(issue["issue_type"] == "director_cue_surface_leak" for issue in result.issues)


def test_quality_guard_retries_generic_refusal_tail_instead_of_accepting_it() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "ゆっくり霊夢、そこは流さない。そこは曖昧にしない",
        msg_type="reply",
        target_char_name="ゆっくり霊夢",
        recent_dialogue_lines=["ゆっくり霊夢: 「上泉ソーマに聞く」"],
        reply_focus_text="提案への返答",
    )

    assert result.action == "retry"
    assert any(issue["issue_type"] == "director_cue_surface_leak" for issue in result.issues)


@pytest.mark.parametrize(
    "text",
    [
        "上泉ソーマ、その話は聞いた",
        "ゆっくり霊夢、話を続けよう",
        "ミリティア、その流れは雑だ",
        "星風ルナ、まだ終わってない",
        "ゆっくり魔理沙、まだ隠してる所を出してよ",
    ],
)
def test_quality_guard_retries_unanchored_reply_fallback_loop_lines(text: str) -> None:
    guard = _make_guard()

    result = guard.evaluate(
        text,
        msg_type="reply",
        target_char_name=text.split("、", maxsplit=1)[0],
        recent_dialogue_lines=["上泉ソーマ: 「……」"],
    )

    assert result.action == "retry"
    assert any(
        issue["issue_type"] in {"director_cue_surface_leak", "generic_reply_tail"}
        for issue in result.issues
    )


def test_quality_guard_does_not_count_generic_refusal_as_proposal_semantic_hit() -> None:
    guard = _make_guard()

    assert (
        guard._reply_focus_family_semantic_hit(
            "そこは流さない",
            focus_family="proposal",
            preferred_action="propose",
        )
        is False
    )


def test_quality_guard_accepts_reply_with_target_name_and_reused_topic() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "星風 瑠奈、その音が拾われないままなのは困る。",
        msg_type="reply",
        target_char_name="星風 瑠奈",
        recent_dialogue_lines=["星風 瑠奈: 「拾われない音は消されるだけです」"],
    )

    assert result.action == "accept"


def test_quality_guard_normalizes_reply_with_soft_question_reaction() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "それって、そこまで切り捨てるってことですか？。",
        msg_type="reply",
        target_char_name="星風 瑠奈",
        recent_dialogue_lines=["星風 瑠奈: 「拾われない音は消されるだけです」"],
    )

    assert result.action == "normalize"
    assert result.text.endswith("？")
    assert any(issue["issue_type"] == "reply_surface_normalized" for issue in result.issues)
    assert any(issue["issue_type"] == "reply_direct_reaction_soft" for issue in result.issues)


def test_quality_guard_requests_retry_for_sentence_overflow() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "それは違う。まだ終わってない。だから今ここで決める。",
        msg_type="reply",
    )

    assert result.action == "shorten"
    assert result.text.count("。") <= 2
    assert any(issue["issue_type"] == "sentence_overflow" for issue in result.issues)


def test_quality_guard_requests_retry_for_monologue_in_conversation_context() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "今日は空気が重い。音だけが逃げる。",
        msg_type="monologue",
        recent_dialogue_lines=["星風 瑠奈: 「まだ帰らないの？」"],
        target_char_name="星風 瑠奈",
    )

    assert result.action == "retry"
    assert any(issue["issue_type"] == "monologue_in_conversation_context" for issue in result.issues)


def test_quality_guard_requests_retry_for_abstract_opening_in_reply() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "気持ちだけが揺れてる。だから先へ進めない。",
        msg_type="reply",
        target_char_name="星風 瑠奈",
        recent_dialogue_lines=["星風 瑠奈: 「まだ帰らないの？」"],
    )

    assert result.action == "retry"
    assert any(issue["issue_type"] == "abstract_opening" for issue in result.issues)


def test_quality_guard_requests_retry_for_low_concreteness_in_monologue() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "何かが違う。まだ遠い。",
        msg_type="monologue",
    )

    assert result.action == "retry"
    assert any(issue["issue_type"] == "low_concreteness" for issue in result.issues)


def test_quality_guard_requests_retry_for_scene_objective_drift() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "窓の外が白い。今日は風が弱い。",
        msg_type="reply",
        target_char_name="星風 瑠奈",
        recent_dialogue_lines=["星風 瑠奈: 「あんた、また逃げるの？」"],
        scene_objective_text="この場の争点: 張り合いが表に出る",
    )

    assert result.action == "retry"
    assert any(issue["issue_type"] == "scene_objective_drift" for issue in result.issues)


def test_quality_guard_requests_retry_for_monologue_scene_objective_drift() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "窓の外が白い。今日は静かだ。",
        msg_type="monologue",
        scene_objective_text="この場の争点: 張り合いが表に出る",
    )

    assert result.action == "retry"
    assert any(issue["issue_type"] == "scene_objective_drift" for issue in result.issues)


def test_quality_guard_requests_retry_for_paragraph_break_output() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "それは違う。\n\n今ここで決める。",
        msg_type="reply",
        target_char_name="星風 瑠奈",
        recent_dialogue_lines=["星風 瑠奈: 「まだ帰らないの？」"],
    )

    assert result.action == "normalize"
    assert "\n" not in result.text
    assert any(issue["issue_type"] == "paragraph_break_output" for issue in result.issues)


def test_quality_guard_still_retries_when_normalized_text_has_hard_issue() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "**気持ち**だけが揺れてる。\n\nだから先へ進めない。",
        msg_type="reply",
        target_char_name="星風 瑠奈",
        recent_dialogue_lines=["星風 瑠奈: 「まだ帰らないの？」"],
    )

    assert result.action == "retry"
    assert any(issue["issue_type"] == "markdown_formatting" for issue in result.issues)
    assert any(issue["issue_type"] == "paragraph_break_output" for issue in result.issues)
    assert any(issue["issue_type"] == "abstract_opening" for issue in result.issues)


def test_quality_guard_requests_retry_for_multi_clause_heaviness() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "暗闇の揺れが胸の奥でこぼれて、静かな運命だけが残る。",
        msg_type="monologue",
    )

    assert result.action == "retry"
    assert any(issue["issue_type"] == "multi_clause_heaviness" for issue in result.issues)


def test_quality_guard_requests_retry_when_reply_focus_is_missing() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "星風 瑠奈、窓の外ばかり見てる。",
        msg_type="reply",
        target_char_name="星風 瑠奈",
        recent_dialogue_lines=["星風 瑠奈: 「まだ帰らないの？」"],
        reply_focus_text="帰るかどうかの確認",
    )

    assert result.action == "retry"
    assert any(issue["issue_type"] == "reply_focus_missing" for issue in result.issues)


def test_quality_guard_requests_retry_when_timing_focus_is_missing() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "星風 瑠奈、基準の話を先に片づける。",
        msg_type="reply",
        target_char_name="星風 瑠奈",
        recent_dialogue_lines=["星風 瑠奈: 「それ、いつ決めるの？」"],
        reply_focus_text="いつ動くかの確認",
    )

    assert result.action == "retry"
    assert any(issue["issue_type"] == "reply_focus_missing" for issue in result.issues)


def test_quality_guard_retries_when_signal_visibility_is_missing() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "上泉ソーマ、まだ帰らない。先に決めることだけ出して。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「まだ帰らないの？」"],
        reply_focus_text="帰るかどうかの確認",
        reply_focus_contract={
            "focus_family": "yes_no",
            "required_tokens": ["帰", "残"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["張り合い", "勝ち負け"],
            "objective_anchor_tokens": [],
            "visibility_mode": "signal_first",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話", "今の件"],
        },
        dominant_signal_text="張り合いに触れると場が動く",
    )

    assert result.action == "retry"
    issue = next(issue for issue in result.issues if issue["issue_type"] == "signal_visibility_missing")
    assert issue["details"]["reply_visibility_mode"] == "signal_first"
    assert issue["details"]["signal_visible"] is False


def test_quality_guard_normalizes_when_objective_visibility_is_only_partially_visible() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "上泉ソーマ、まだ帰らない。今の件だけ先に決める。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「まだ帰らないの？」"],
        reply_focus_text="帰るかどうかの確認",
        reply_focus_contract={
            "focus_family": "yes_no",
            "required_tokens": ["帰", "残"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["張り合い", "勝ち負け"],
            "objective_anchor_tokens": ["順番", "決める"],
            "visibility_mode": "paired",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["今の件", "その話"],
        },
        dominant_signal_text="張り合いに触れると場が動く",
        scene_objective_text="この場の争点: 順番を決める",
    )

    assert result.action == "normalize"
    issue = next(
        issue for issue in result.issues if issue["issue_type"] == "scene_objective_visibility_missing"
    )
    assert issue["details"]["reply_visibility_mode"] == "paired"
    assert issue["details"]["objective_visible"] is False


def test_quality_guard_accepts_reply_that_answers_timing_focus() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "星風 瑠奈、いつ決めるかだけ今ここで出す。",
        msg_type="reply",
        target_char_name="星風 瑠奈",
        recent_dialogue_lines=["星風 瑠奈: 「それ、いつ決めるの？」"],
        reply_focus_text="いつ動くかの確認",
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "reply_focus_missing" for issue in result.issues)


def test_quality_guard_accepts_timing_focus_via_focus_cue_without_required_token_hit() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "時間だけ先に出す。順番はそのあとだ。",
        msg_type="reply",
        target_char_name="星風 瑠奈",
        recent_dialogue_lines=["星風 瑠奈: 「それ、いつ決めるの？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "後", "あと", "明日", "決め"],
            "required_focus_cues": ["先に", "あとで", "いつ"],
            "focus_anchor_tokens": ["先に", "あとで"],
            "forbidden_focus_drift_tokens": ["基準", "誰が"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "reply_focus_missing" for issue in result.issues)


def test_quality_guard_retries_decision_owner_focus_when_only_order_token_is_present() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "順番だけ先に出す。基準はそのあとだ。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「誰が決めるんだ？」"],
        reply_focus_text="誰が決めるかの確認",
        reply_focus_contract={
            "focus_family": "decision_owner",
            "required_tokens": ["誰", "決め", "役", "順番"],
            "required_focus_cues": ["誰が", "私が", "お前が", "任せる"],
            "focus_anchor_tokens": ["誰が", "私が", "お前が"],
            "forbidden_focus_drift_tokens": ["基準", "見せ場"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
    )

    assert result.action == "retry"
    focus_issue = next(issue for issue in result.issues if issue["issue_type"] == "reply_focus_missing")
    assert focus_issue["details"]["reply_focus_family"] == "decision_owner"
    assert focus_issue["details"]["reply_focus_anchor_seen"] is False
    assert focus_issue["details"]["reply_focus_semantic_missed"] is True


def test_quality_guard_accepts_decision_owner_focus_via_role_owner_cue() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "決める役はこっちが持つ。順番はそのあとで決める。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「誰が決めるんだ？」"],
        reply_focus_text="誰が決めるかの確認",
        reply_focus_contract={
            "focus_family": "decision_owner",
            "required_tokens": ["誰", "決め", "役", "順番"],
            "required_focus_cues": ["誰が", "こっち", "私が", "お前が", "任せる", "決める役"],
            "focus_anchor_tokens": ["誰が", "こっち", "私が", "お前が", "決める役"],
            "forbidden_focus_drift_tokens": ["基準", "見せ場"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "reply_focus_missing" for issue in result.issues)
    assert not any(issue["issue_type"] == "reply_without_direct_reaction" for issue in result.issues)


def test_quality_guard_accepts_question_that_reuses_dialogue_topic() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "その『見せ場』って何の基準？ただの勢いの差？",
        msg_type="reply",
        target_char_name="ゆっくり霊夢",
        recent_dialogue_lines=["ゆっくり霊夢: 「この見せ場、誰の基準で決めるの？」"],
        scene_objective_text="この場の争点: 張り合いが表に出る",
        dominant_signal_text="張り合いに触れると場が動く",
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "reply_without_direct_reaction" for issue in result.issues)
    assert not any(issue["issue_type"] == "scene_objective_drift" for issue in result.issues)


def test_quality_guard_accepts_question_for_decision_owner_focus() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "ソーマの『済む話』って、誰がどのコード進めるって決めるの？",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「残りを言えば済む話だ」"],
        reply_focus_text="誰が決めるかの確認",
        scene_objective_text="この場の争点: 張り合いが表に出る",
        dominant_signal_text="張り合いに触れると場が動く",
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "reply_focus_missing" for issue in result.issues)
    assert not any(issue["issue_type"] == "scene_objective_drift" for issue in result.issues)


def test_quality_guard_accepts_focus_family_direct_reaction_without_name_reuse() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "誰が決めるかだけ先に出して。順番はそのあとでいい。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「残りを言えば済む話だ」"],
        reply_focus_text="誰が決めるかの確認",
        scene_objective_text="この場の争点: 張り合いが表に出る",
        dominant_signal_text="張り合いに触れると場が動く",
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "reply_without_direct_reaction" for issue in result.issues)
    assert not any(issue["issue_type"] == "reply_focus_missing" for issue in result.issues)


def test_quality_guard_accepts_timing_direct_reaction_via_family_cue() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "今決める。順番はそのあとだ。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「それ、いつ決めるんだ？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "後", "あと", "明日", "決め"],
            "required_focus_cues": ["今", "先に", "あとで", "いつ"],
            "focus_anchor_tokens": ["今", "先に", "あとで"],
            "forbidden_focus_drift_tokens": ["基準", "誰が"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "reply_without_direct_reaction" for issue in result.issues)


def test_quality_guard_accepts_timing_focus_via_fixed_before_start_family_cue() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "始まる前に固定する。順番はそのあとでいい。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「それ、いつ決めるんだ？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "後", "あと", "明日", "決め"],
            "required_focus_cues": ["今", "先に", "あとで", "いつ"],
            "focus_anchor_tokens": ["今", "先に", "あとで"],
            "forbidden_focus_drift_tokens": ["基準", "誰が"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "reply_focus_missing" for issue in result.issues)


def test_quality_guard_accepts_timing_focus_via_timing_alignment_cue() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "タイミングだけ先に合わせる。順番はそのあとでいい。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「それ、いつ決めるんだ？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "後", "あと", "明日", "決め"],
            "required_focus_cues": ["今", "先に", "あとで", "いつ"],
            "focus_anchor_tokens": ["今", "先に", "あとで"],
            "forbidden_focus_drift_tokens": ["基準", "誰が"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "reply_focus_missing" for issue in result.issues)


def test_quality_guard_accepts_timing_focus_via_pre_move_cue() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "動く前にタイミングだけ合わせる。順番はそのあとでいい。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「それ、いつ決めるんだ？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "後", "あと", "明日", "決め"],
            "required_focus_cues": ["今", "先に", "あとで", "いつ"],
            "focus_anchor_tokens": ["今", "先に", "あとで"],
            "forbidden_focus_drift_tokens": ["基準", "誰が"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "reply_focus_missing" for issue in result.issues)


def test_quality_guard_accepts_decision_owner_focus_via_title_cue() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "誰の肩書きで決めるか先に出せ。見せ場はこっちが通す。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「誰が決めるんだ？」"],
        reply_focus_text="誰が決めるかの確認",
        reply_focus_contract={
            "focus_family": "decision_owner",
            "required_tokens": ["誰", "決め", "役"],
            "required_focus_cues": ["誰が", "こっち", "私が", "お前が", "任せる", "決める役"],
            "focus_anchor_tokens": ["誰が", "こっち", "私が", "お前が", "決める役"],
            "forbidden_focus_drift_tokens": ["基準", "見せ場"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "reply_focus_missing" for issue in result.issues)


def test_quality_guard_accepts_decision_owner_focus_via_authority_cue() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "誰がその定義を出す権利があるのか、一度整理しよう。見せ場はまだ渡さない。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「誰が決めるんだ？」"],
        reply_focus_text="誰が決めるかの確認",
        reply_focus_contract={
            "focus_family": "decision_owner",
            "required_tokens": ["誰", "決め", "役"],
            "required_focus_cues": ["誰が", "こっち", "私が", "お前が", "任せる", "決める役"],
            "focus_anchor_tokens": ["誰が", "こっち", "私が", "お前が", "決める役"],
            "forbidden_focus_drift_tokens": ["基準", "見せ場"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "reply_focus_missing" for issue in result.issues)


def test_quality_guard_accepts_decision_owner_focus_via_authority_right_cue() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "誰にその権利があるか先に決める。主導権はまだ渡さない。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「誰が決めるんだ？」"],
        reply_focus_text="誰が決めるかの確認",
        reply_focus_contract={
            "focus_family": "decision_owner",
            "required_tokens": ["誰", "決め", "役"],
            "required_focus_cues": ["誰が", "こっち", "私が", "お前が", "任せる", "決める役", "権利", "主導権"],
            "focus_anchor_tokens": ["誰が", "こっち", "私が", "お前が", "決める役", "権利", "主導権"],
            "forbidden_focus_drift_tokens": ["基準", "見せ場"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "reply_focus_missing" for issue in result.issues)


def test_quality_guard_accepts_proposal_focus_via_redirect_family_cue() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "じゃあ順番を変える。先にコード表だけ出して。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「そのまま押し切ろう」"],
        reply_focus_text="相手の提案への返答",
        reply_focus_contract={
            "focus_family": "proposal",
            "required_tokens": ["なら", "じゃあ", "どう", "先に"],
            "required_focus_cues": ["なら", "じゃあ", "先に", "どう"],
            "focus_anchor_tokens": ["なら", "じゃあ"],
            "preferred_action": "propose",
            "must_answer_in_first_sentence": True,
        },
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "reply_focus_missing" for issue in result.issues)


def test_quality_guard_accepts_basis_focus_via_measure_cue() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "何で測るかだけ先に決める。見せ場の順番はそのあとでいい。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「その見せ場、誰の基準で決めるんだ？」"],
        reply_focus_text="判断基準の確認",
        reply_focus_contract={
            "focus_family": "basis",
            "required_tokens": ["基準", "何", "どの"],
            "required_focus_cues": ["基準", "何の", "どの", "何で"],
            "focus_anchor_tokens": ["基準", "何の", "どの"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "reply_focus_missing" for issue in result.issues)


def test_quality_guard_accepts_basis_focus_via_judgment_basis_cue() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "判断基準は音の必然性でいい。主導権はここで決める。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「その見せ場、誰の基準で決めるんだ？」"],
        reply_focus_text="判断基準の確認",
        reply_focus_contract={
            "focus_family": "basis",
            "required_tokens": ["基準", "何", "どの"],
            "required_focus_cues": ["基準", "何の", "どの", "何で", "どの基準", "判断基準"],
            "focus_anchor_tokens": ["基準", "何の", "どの", "どの基準", "判断基準"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "reply_focus_missing" for issue in result.issues)


def test_quality_guard_suppresses_basis_objective_visibility_when_basis_cue_is_strong() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "どの基準で決めるか先に言え。主導権だけはここで握る。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「その見せ場、誰の基準で決めるんだ？」"],
        reply_focus_text="判断基準の確認",
        reply_focus_contract={
            "focus_family": "basis",
            "required_tokens": ["基準", "何", "どの"],
            "required_focus_cues": ["基準", "何の", "どの", "何で", "どの基準"],
            "focus_anchor_tokens": ["基準", "何の", "どの", "どの基準"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["主導権"],
            "objective_anchor_tokens": ["見せ場", "勝負"],
            "visibility_mode": "paired",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
    )

    assert not any(issue["issue_type"] == "scene_objective_visibility_missing" for issue in result.issues)


def test_quality_guard_suppresses_basis_process_objective_visibility_when_focus_is_strong() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "判断基準は音の必然性でいい。流れなら主導権をここで決めろ。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「その見せ場、誰の基準で決めるんだ？」"],
        reply_focus_text="判断基準の確認",
        reply_focus_contract={
            "focus_family": "basis",
            "required_tokens": ["基準", "何", "どの"],
            "required_focus_cues": ["基準", "何の", "どの", "何で", "どの基準"],
            "focus_anchor_tokens": ["基準", "何の", "どの", "どの基準"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": [],
            "objective_anchor_tokens": ["流れ", "回収"],
            "visibility_mode": "objective_first",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
    )

    assert not any(issue["issue_type"] == "scene_objective_visibility_missing" for issue in result.issues)


def test_quality_guard_suppresses_basis_control_objective_visibility_when_focus_is_strong() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "判断基準は音の必然性でいい。入れ替わりの主導権はここで決める。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「その見せ場、誰の基準で決めるんだ？」"],
        reply_focus_text="判断基準の確認",
        reply_focus_contract={
            "focus_family": "basis",
            "required_tokens": ["基準", "何", "どの"],
            "required_focus_cues": ["基準", "何の", "どの", "何で", "どの基準", "判断基準"],
            "focus_anchor_tokens": ["基準", "何の", "どの", "どの基準", "判断基準"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": [],
            "objective_anchor_tokens": ["主導権", "入れ替わり"],
            "visibility_mode": "objective_first",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
    )

    assert not any(issue["issue_type"] == "scene_objective_visibility_missing" for issue in result.issues)


def test_quality_guard_suppresses_basis_flow_control_visibility_when_focus_is_strong() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "判断基準は音の必然性でいい。入れ替わりの主導権はここで決める。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「その見せ場、誰の基準で決めるんだ？」"],
        reply_focus_text="判断基準の確認",
        reply_focus_contract={
            "focus_family": "basis",
            "required_tokens": ["基準", "何", "どの"],
            "required_focus_cues": ["基準", "何の", "どの", "何で", "どの基準", "判断基準"],
            "focus_anchor_tokens": ["基準", "何の", "どの", "どの基準", "判断基準"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["流れ", "回収"],
            "objective_anchor_tokens": ["主導権", "入れ替わり"],
            "visibility_mode": "paired",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
    )

    assert not any(issue["issue_type"] == "signal_visibility_missing" for issue in result.issues)
    assert not any(issue["issue_type"] == "scene_objective_visibility_missing" for issue in result.issues)


def test_quality_guard_normalizes_basis_process_visibility_when_basis_focus_is_strong() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "判断基準は音の必然性でいい。主導権をここで決めろ。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「その見せ場、誰の基準で決めるんだ？」"],
        reply_focus_text="判断基準の確認",
        reply_focus_contract={
            "focus_family": "basis",
            "required_tokens": ["基準", "何", "どの"],
            "required_focus_cues": ["基準", "何の", "どの", "何で", "どの基準"],
            "focus_anchor_tokens": ["基準", "何の", "どの", "どの基準"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["見せ場"],
            "objective_anchor_tokens": ["流れ", "回収"],
            "visibility_mode": "paired",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
    )

    assert result.action == "normalize"
    signal_issue = next(issue for issue in result.issues if issue["issue_type"] == "signal_visibility_missing")
    objective_issue = next(
        issue for issue in result.issues if issue["issue_type"] == "scene_objective_visibility_missing"
    )
    assert signal_issue["severity"] == "info"
    assert objective_issue["severity"] == "info"
    assert signal_issue["details"]["visibility_blocking"] is False
    assert objective_issue["details"]["visibility_blocking"] is False


def test_quality_guard_normalizes_basis_overlapping_dramatic_visibility_when_focus_is_strong() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "判断基準は音の必然性でいい。主導権はここで決める。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「その見せ場、誰の基準で決めるんだ？」"],
        reply_focus_text="判断基準の確認",
        reply_focus_contract={
            "focus_family": "basis",
            "required_tokens": ["基準", "何", "どの"],
            "required_focus_cues": ["基準", "何の", "どの", "何で", "どの基準", "判断基準"],
            "focus_anchor_tokens": ["基準", "何の", "どの", "どの基準", "判断基準"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["見せ場"],
            "objective_anchor_tokens": ["見せ場", "勝負"],
            "visibility_mode": "paired",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
    )

    assert result.action == "accept"
    assert not any(issue["issue_type"] == "signal_visibility_missing" for issue in result.issues)
    assert not any(issue["issue_type"] == "scene_objective_visibility_missing" for issue in result.issues)


def test_quality_guard_suppresses_basis_overlapping_dramatic_visibility_when_focus_is_strong() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "判断基準は音の必然性でいい。主導権はここで決める。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「その見せ場、誰の基準で決めるんだ？」"],
        reply_focus_text="判断基準の確認",
        reply_focus_contract={
            "focus_family": "basis",
            "required_tokens": ["基準", "何", "どの"],
            "required_focus_cues": ["基準", "何の", "どの", "何で", "どの基準", "判断基準"],
            "focus_anchor_tokens": ["基準", "何の", "どの", "どの基準", "判断基準"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["見せ場"],
            "objective_anchor_tokens": ["見せ場", "勝負"],
            "visibility_mode": "paired",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
    )

    assert not any(issue["issue_type"] == "signal_visibility_missing" for issue in result.issues)
    assert not any(issue["issue_type"] == "scene_objective_visibility_missing" for issue in result.issues)


def test_quality_guard_accepts_timing_focus_via_flow_cut_cue() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "流れをどこで切るか先に出して。今の押し方はそのあとで決める。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「それ、いつ決めるんだ？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "後", "あと", "明日", "決め"],
            "required_focus_cues": ["今", "先に", "あとで", "いつ"],
            "focus_anchor_tokens": ["今", "先に", "あとで"],
            "forbidden_focus_drift_tokens": ["基準", "誰が"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "reply_focus_missing" for issue in result.issues)


def test_quality_guard_accepts_timing_focus_via_current_timing_owner_cue() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "今のタイミングで主導権を取るのは、あたしが先に決めるでしょ？見せ場をどこにするか、あたしが一度決め直すわね。",
        msg_type="reply",
        target_char_name="ミリティア",
        recent_dialogue_lines=["ミリティア: 「それ、いつ決めるの？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "後", "あと", "明日", "決め"],
            "required_focus_cues": ["今", "先に", "あとで", "いつ", "タイミング"],
            "focus_anchor_tokens": ["今", "先に", "あとで", "タイミング"],
            "forbidden_focus_drift_tokens": ["基準", "誰が"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
    )

    assert not any(issue["issue_type"] == "reply_focus_missing" for issue in result.issues)


def test_quality_guard_accepts_timing_focus_via_now_while_location_fixing_cue() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "今のうち、構造的な必然性より場所の固定を先に決めよう。見せ場をどこに置くか、先に決定しないと次の勝負が始まらないぞ。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「それ、いつ決めるんだ？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "後", "あと", "明日", "決め"],
            "required_focus_cues": ["今", "先に", "あとで", "いつ", "タイミング"],
            "focus_anchor_tokens": ["今", "先に", "あとで", "タイミング"],
            "forbidden_focus_drift_tokens": ["基準", "誰が"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
    )

    assert not any(issue["issue_type"] == "reply_focus_missing" for issue in result.issues)


def test_quality_guard_accepts_timing_focus_via_pause_or_cut_cue() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "今の流れはここで一回止める。そのあとで順番を決める。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「それ、いつ決めるんだ？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "後", "あと", "明日", "決め"],
            "required_focus_cues": ["今", "先に", "あとで", "いつ", "タイミング"],
            "focus_anchor_tokens": ["今", "先に", "あとで", "タイミング"],
            "forbidden_focus_drift_tokens": ["基準", "誰が"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
    )

    assert not any(issue["issue_type"] == "reply_focus_missing" for issue in result.issues)


def test_quality_guard_accepts_timing_focus_via_order_first_cue() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "今、順番は先に決めてから話す。主導権を誰が取るか先に言え。",
        msg_type="reply",
        target_char_name="ちゅるるん",
        recent_dialogue_lines=["ちゅるるん: 「それ、いつ決めるの？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "後", "あと", "明日", "決め"],
            "required_focus_cues": ["今", "先に", "あとで", "いつ", "タイミング"],
            "focus_anchor_tokens": ["今", "先に", "あとで", "タイミング"],
            "forbidden_focus_drift_tokens": ["基準", "誰が"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
    )

    assert not any(issue["issue_type"] == "reply_focus_missing" for issue in result.issues)


def test_quality_guard_suppresses_timing_objective_visibility_when_timing_cue_is_strong() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "流れをどこで切るか先に出して。今の押し方はそのあとで決める。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「それ、いつ決めるんだ？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "後", "あと", "明日", "決め"],
            "required_focus_cues": ["今", "先に", "あとで", "いつ"],
            "focus_anchor_tokens": ["今", "先に", "あとで"],
            "forbidden_focus_drift_tokens": ["基準", "誰が"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": [],
            "objective_anchor_tokens": ["流れ", "回収"],
            "visibility_mode": "objective_first",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
    )

    assert not any(issue["issue_type"] == "scene_objective_visibility_missing" for issue in result.issues)


def test_quality_guard_suppresses_timing_signal_visibility_when_control_objective_is_visible() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "今、順番は先に決めてから話す。主導権を誰が取るか先に言え。",
        msg_type="reply",
        target_char_name="ちゅるるん",
        recent_dialogue_lines=["ちゅるるん: 「それ、いつ決めるの？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "後", "あと", "明日", "決め"],
            "required_focus_cues": ["今", "先に", "あとで", "いつ", "タイミング"],
            "focus_anchor_tokens": ["今", "先に", "あとで", "タイミング"],
            "forbidden_focus_drift_tokens": ["基準", "誰が"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["流れ", "回収"],
            "objective_anchor_tokens": ["主導権", "入れ替わり"],
            "visibility_mode": "paired",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
    )

    assert not any(issue["issue_type"] == "signal_visibility_missing" for issue in result.issues)
    assert not any(issue["issue_type"] == "scene_objective_visibility_missing" for issue in result.issues)


def test_quality_guard_suppresses_basis_objective_first_dramatic_visibility_when_focus_is_strong() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "判断基準は音の必然性でいい。主導権はここで決める。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「その見せ場、誰の基準で決めるんだ？」"],
        reply_focus_text="判断基準の確認",
        reply_focus_contract={
            "focus_family": "basis",
            "required_tokens": ["基準", "何", "どの"],
            "required_focus_cues": ["基準", "何の", "どの", "何で", "どの基準", "判断基準"],
            "focus_anchor_tokens": ["基準", "何の", "どの", "どの基準", "判断基準"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": [],
            "objective_anchor_tokens": ["見せ場", "勝負"],
            "visibility_mode": "objective_first",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
    )

    assert not any(issue["issue_type"] == "scene_objective_visibility_missing" for issue in result.issues)


def test_quality_guard_suppresses_basis_signal_visibility_when_control_objective_is_visible() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "判断基準は音の必然性でいい。主導権はここで決める。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「その見せ場、誰の基準で決めるんだ？」"],
        reply_focus_text="判断基準の確認",
        reply_focus_contract={
            "focus_family": "basis",
            "required_tokens": ["基準", "何", "どの"],
            "required_focus_cues": ["基準", "何の", "どの", "何で", "どの基準", "判断基準"],
            "focus_anchor_tokens": ["基準", "何の", "どの", "どの基準", "判断基準"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["見せ場"],
            "objective_anchor_tokens": ["主導権", "入れ替わり"],
            "visibility_mode": "paired",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
    )

    assert not any(issue["issue_type"] == "signal_visibility_missing" for issue in result.issues)
    assert not any(issue["issue_type"] == "scene_objective_visibility_missing" for issue in result.issues)


def test_quality_guard_accepts_basis_focus_via_definition_cue() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "その見せ場の定義を、誰が確定するんですか。順番を先に固定しないと話が散ります。",
        msg_type="reply",
        target_char_name="ミリティア",
        recent_dialogue_lines=["ミリティア: 「その見せ場、誰の基準で決めるつもり？」"],
        reply_focus_text="判断基準の確認",
        reply_focus_contract={
            "focus_family": "basis",
            "required_tokens": ["基準", "何", "どの"],
            "required_focus_cues": ["基準", "何の", "どの", "何で", "どの基準", "判断基準"],
            "focus_anchor_tokens": ["基準", "何の", "どの", "どの基準", "判断基準"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
    )

    assert not any(issue["issue_type"] == "reply_focus_missing" for issue in result.issues)


def test_quality_guard_normalizes_timing_dramatic_objective_visibility_when_timing_cue_is_strong() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "今ここで決める。順番だけ合わせて、そのあと動かす。",
        msg_type="reply",
        target_char_name="星風 瑠奈",
        recent_dialogue_lines=["星風 瑠奈: 「それ、いつ決めるの？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "後", "あと", "明日", "決め"],
            "required_focus_cues": ["今", "先に", "あとで", "いつ", "タイミング"],
            "focus_anchor_tokens": ["今", "先に", "あとで", "タイミング"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": [],
            "objective_anchor_tokens": ["見せ場", "勝負"],
            "visibility_mode": "objective_first",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
    )

    assert result.action == "normalize"
    objective_issue = next(
        issue for issue in result.issues if issue["issue_type"] == "scene_objective_visibility_missing"
    )
    assert objective_issue["severity"] == "info"
    assert objective_issue["details"]["visibility_blocking"] is False
    assert objective_issue["details"]["reply_focus_first_sentence_semantic_hit"] is True


def test_quality_guard_suppresses_timing_visibility_when_signal_and_objective_overlap() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "いつ決めるかを今ここで決める。見せ場をどこに置くか先に出して。",
        msg_type="reply",
        target_char_name="夜風ユウマ",
        recent_dialogue_lines=["夜風ユウマ: 「それ、いつ決めるんだ？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "後", "あと", "明日", "決め"],
            "required_focus_cues": ["今", "先に", "あとで", "いつ"],
            "focus_anchor_tokens": ["今", "先に", "あとで"],
            "forbidden_focus_drift_tokens": ["基準", "誰が"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["見せ場"],
            "objective_anchor_tokens": ["見せ場", "勝負"],
            "visibility_mode": "paired",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
    )

    assert not any(issue["issue_type"] == "signal_visibility_missing" for issue in result.issues)
    assert not any(issue["issue_type"] == "scene_objective_visibility_missing" for issue in result.issues)


def test_quality_guard_suppresses_timing_scene_priority_visibility_when_order_cue_is_strong() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "今、順番は先に決めてから話す。主導権を誰が取るか先に言え。",
        msg_type="reply",
        target_char_name="ちゅるるん",
        recent_dialogue_lines=["ちゅるるん: 「それ、いつ決めるの？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "後", "あと", "明日", "決め"],
            "required_focus_cues": ["今", "先に", "あとで", "いつ", "タイミング"],
            "focus_anchor_tokens": ["今", "先に", "あとで", "タイミング"],
            "forbidden_focus_drift_tokens": ["基準", "誰が"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["見せ場"],
            "objective_anchor_tokens": ["流れ", "回収"],
            "visibility_mode": "paired",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
    )

    assert not any(issue["issue_type"] == "signal_visibility_missing" for issue in result.issues)
    assert not any(issue["issue_type"] == "scene_objective_visibility_missing" for issue in result.issues)


def test_quality_guard_treats_timing_story_flavor_weak_with_probe_pressure_as_nonblocking() -> None:
    assert QualityGuard._is_blocking_voice_flat_details(
        {
            "flat_issue_family": "story_flavor_weak",
            "reply_focus_family": "timing",
            "reply_shape_mode": "answer_then_probe",
            "reply_pressure_shift_seen": True,
            "reply_story_pressure_seen": False,
            "story_flavor_weak": True,
            "reply_soft_landing_used": False,
            "dramatic_contract_missed": True,
        }
    ) is False


def test_quality_guard_retries_when_focus_is_present_but_direct_reaction_is_generic() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "基準の話はまた別だ。整理だけしておく。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「誰が決めるんだ？」"],
        reply_focus_text="誰が決めるかの確認",
        reply_focus_contract={
            "focus_family": "decision_owner",
            "required_tokens": ["誰", "決め", "役", "順番"],
            "required_focus_cues": ["誰が", "私が", "お前が", "任せる"],
            "focus_anchor_tokens": ["誰が", "私が", "お前が"],
            "forbidden_focus_drift_tokens": ["基準", "見せ場"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
    )

    assert result.action == "retry"
    issue = next(issue for issue in result.issues if issue["issue_type"] == "reply_without_direct_reaction")
    assert issue["details"]["reply_focus_family"] == "decision_owner"
    assert issue["details"]["reply_direct_reaction_mode"] == "hard_retry"
    assert issue["details"]["reply_direct_anchor_seen"] is False


def test_quality_guard_normalizes_generic_reply_tail_when_reply_is_otherwise_sound() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "星風 瑠奈、まだ帰らない。今の流れをここで返す。",
        msg_type="reply",
        target_char_name="星風 瑠奈",
        recent_dialogue_lines=["星風 瑠奈: 「まだ帰らないの？」"],
        reply_focus_text="帰るかどうかの確認",
    )

    assert result.action == "normalize"
    assert any(issue["issue_type"] == "generic_reply_tail" for issue in result.issues)


def test_quality_guard_retries_empty_question_loop_tail() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "ミリティア、その流れは雑だ。どうするの？",
        msg_type="reply",
        target_char_name="ミリティア",
        recent_dialogue_lines=["ミリティア: 「夜風ユウマ、もう少し聞かせてよ。どうするの？」"],
    )

    assert result.action == "retry"
    issue = next(issue for issue in result.issues if issue["issue_type"] == "generic_reply_tail")
    assert issue["details"]["generic_reply_tail_blocking"] is True


def test_quality_guard_ignores_generic_reply_focus_placeholder() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "上泉ソーマ、それは流さない。見せ場をどこに置くか先に出して。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「そのまま押し切るつもりだ」"],
        reply_focus_text="相手の主張への返答",
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "reply_focus_missing" for issue in result.issues)


def test_quality_guard_accepts_reply_with_direct_reaction_and_scene_push() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "星風 瑠奈、まだ帰らない。ここで決めるなら先に言って。",
        msg_type="reply",
        target_char_name="星風 瑠奈",
        recent_dialogue_lines=["星風 瑠奈: 「まだ帰らないの？」"],
        scene_objective_text="この場の争点: 張り合いが表に出る",
        reply_focus_text="帰るかどうかの確認",
    )

    assert result.action != "retry"
    assert all(issue["issue_type"] != "scene_objective_drift" for issue in result.issues)


def test_quality_guard_leniency_can_downgrade_retry_to_accept() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "暗闇の運命が静かに揺れ、本音だけが空っぽにこぼれていく。",
        msg_type="monologue",
        quality_leniency={"poetic_abstraction": 1.0, "multi_clause_heaviness": 0.8},
    )

    assert result.action == "accept"
    assert any(issue["issue_type"] == "poetic_abstraction" for issue in result.issues)


def test_quality_guard_leniency_can_downgrade_drop_to_retry() -> None:
    guard = _make_guard(max_abstract_token_occurrences=2)

    result = guard.evaluate(
        "誰かのために誰かが誰かへ誰かを語る。",
        msg_type="group",
        quality_leniency={"abstract_repetition": 1.0},
    )

    assert result.action == "retry"
    assert any(issue["issue_type"] == "abstract_repetition" for issue in result.issues)


def test_quality_guard_ignores_known_fallback_second_sentence_as_generic_tail() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "星風 瑠奈、そこは流さない。そこは今ここで決める。",
        msg_type="reply",
        target_char_name="星風 瑠奈",
        recent_dialogue_lines=["星風 瑠奈: 「まだ帰らないの？」"],
    )

    assert all(issue["issue_type"] != "generic_reply_tail" for issue in result.issues)


def test_quality_guard_prioritizes_focus_miss_over_voice_flat_reply() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "星風 瑠奈、それは違う。確認します。",
        msg_type="reply",
        target_char_name="星風 瑠奈",
        recent_dialogue_lines=["星風 瑠奈: 「まだ帰らないの？」"],
        reply_focus_text="帰るかどうかの確認",
        voice_anchor_text="熱血直球で短く返す",
    )

    assert result.action == "retry"
    assert any(issue["issue_type"] == "reply_focus_missing" for issue in result.issues)
    assert not any(issue["issue_type"] == "voice_flat_reply" for issue in result.issues)


def test_quality_guard_allows_question_reply_with_plain_confirmation_tail() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "その見せ場って誰の基準？確認します。",
        msg_type="reply",
        target_char_name="ゆっくり霊夢",
        recent_dialogue_lines=["ゆっくり霊夢: 「この見せ場、誰の基準で決めるの？」"],
        reply_focus_text="判断基準の確認",
        scene_objective_text="この場の争点: 張り合いが表に出る",
        dominant_signal_text="張り合いに触れると場が動く",
        voice_anchor_text="熱血直球で短く返す",
    )

    assert result.action == "normalize"
    assert not any(issue["issue_type"] == "voice_flat_reply" for issue in result.issues)


def test_quality_guard_accepts_focus_recovered_in_second_sentence() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "それは違う。誰が決めるかだけ先に出して。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「残りを言えば済む話だ」"],
        reply_focus_text="誰が決めるかの確認",
        scene_objective_text="この場の争点: 張り合いが表に出る",
        dominant_signal_text="張り合いに触れると場が動く",
    )

    assert result.action != "retry"
    assert not any(issue["issue_type"] == "reply_focus_missing" for issue in result.issues)


def test_quality_guard_normalizes_soft_direct_reaction_when_focus_contract_is_satisfied() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "いつ決めるかだけ今ここで出す。順番はそのあとでいい。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「それ、いつ決めるんだ？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "決め"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
    )

    assert result.action == "normalize"
    assert any(issue["issue_type"] == "reply_direct_reaction_soft" for issue in result.issues)
    assert not any(issue["issue_type"] == "reply_without_direct_reaction" for issue in result.issues)


def test_quality_guard_keeps_focus_missing_as_hard_retry_even_with_contract() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "それは違う。確認します。",
        msg_type="reply",
        target_char_name="星風 瑠奈",
        recent_dialogue_lines=["星風 瑠奈: 「まだ帰らないの？」"],
        reply_focus_text="帰るかどうかの確認",
        reply_focus_contract={
            "focus_family": "yes_no",
            "required_tokens": ["帰", "残"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
    )

    assert result.action == "retry"
    assert any(issue["issue_type"] == "reply_focus_missing" for issue in result.issues)


def test_quality_guard_retries_when_recent_ending_is_reused_without_variety_shift() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "星風 瑠奈、まだ帰らない。順番はそのあとでいい。",
        msg_type="reply",
        target_char_name="星風 瑠奈",
        recent_dialogue_lines=["星風 瑠奈: 「まだ帰らないの？」"],
        reply_focus_text="帰るかどうかの確認",
        reply_focus_contract={
            "focus_family": "yes_no",
            "required_tokens": ["帰", "残"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_variety_contract={
            "response_shape": "answer_then_press",
            "second_beat_mode": "press",
            "forbidden_recent_openings": [],
            "forbidden_recent_endings": ["順番はそのあとでいい"],
            "preferred_move_tokens": ["先に", "言って"],
        },
    )

    assert result.action == "retry"
    voice_issue = next(issue for issue in result.issues if issue["issue_type"] == "voice_flat_reply")
    assert voice_issue["details"]["recent_ending_reused"] is True
    assert voice_issue["details"]["reply_variety_second_beat"] == "press"


def test_quality_guard_normalizes_when_opening_reuse_has_varied_second_beat() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "星風 瑠奈、まだ帰らない。先に言って。",
        msg_type="reply",
        target_char_name="星風 瑠奈",
        recent_dialogue_lines=["星風 瑠奈: 「まだ帰らないの？」"],
        reply_focus_text="帰るかどうかの確認",
        reply_focus_contract={
            "focus_family": "yes_no",
            "required_tokens": ["帰", "残"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_variety_contract={
            "response_shape": "answer_then_press",
            "second_beat_mode": "press",
            "forbidden_recent_openings": ["星風 瑠奈、まだ帰らない"],
            "forbidden_recent_endings": ["順番はそのあとでいい"],
            "preferred_move_tokens": ["先に", "言って"],
        },
    )

    assert result.action == "normalize"
    voice_issue = next(issue for issue in result.issues if issue["issue_type"] == "voice_flat_reply")
    assert voice_issue["details"]["recent_opening_reused"] is True
    assert voice_issue["details"]["recent_ending_reused"] is False


def test_quality_guard_retries_when_second_beat_is_reused_without_pressure_shift() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "星風 瑠奈、まだ帰らない。先に言って。",
        msg_type="reply",
        target_char_name="星風 瑠奈",
        recent_dialogue_lines=["星風 瑠奈: 「まだ帰らないの？」"],
        reply_focus_text="帰るかどうかの確認",
        reply_focus_contract={
            "focus_family": "yes_no",
            "required_tokens": ["帰", "残"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["見せ場"],
            "objective_anchor_tokens": [],
            "visibility_mode": "signal_first",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
        reply_variety_contract={
            "focus_family": "yes_no",
            "response_shape": "answer_then_press",
            "second_beat_mode": "press",
            "forbidden_recent_openings": [],
            "forbidden_recent_endings": [],
            "preferred_move_tokens": ["先に", "言って"],
            "recent_second_beat_history": ["press", "press"],
            "forbidden_recent_second_beats": ["press"],
        },
        reply_dramatic_contract={
            "move_mode": "counter",
            "allowed_secondary_modes": ["probe"],
            "required_move_tokens": ["先に", "言って"],
            "must_change_pressure_in_second_sentence": True,
            "forbidden_soft_landings": ["順番はそのあとでいい"],
        },
        reply_shape_contract={
            "primary_shape": "answer_then_counter",
            "secondary_shape": "answer_then_probe",
            "must_shift_pressure_in_second_sentence": True,
            "forbidden_soft_landings": ["順番はそのあとでいい"],
            "recent_shape_history": ["answer_then_press", "answer_then_press"],
            "story_pressure_tokens": ["張り合い", "順番"],
        },
    )

    assert result.action == "retry"
    voice_issue = next(issue for issue in result.issues if issue["issue_type"] == "voice_flat_reply")
    assert voice_issue["details"]["recent_second_beat_reused"] is True
    assert voice_issue["details"]["variety_contract_missed"] is True


def test_quality_guard_retries_when_quality_contract_is_missed_after_signal_visibility() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "星風 瑠奈、今決める。張り合いも順番も先に見ておく。",
        msg_type="reply",
        target_char_name="星風 瑠奈",
        recent_dialogue_lines=["星風 瑠奈: 「それ、いつ決めるの？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "決め"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["見せ場"],
            "objective_anchor_tokens": [],
            "visibility_mode": "signal_first",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
        reply_quality_contract={
            "primary_shape": "answer_then_probe",
            "second_beat_mode": "press",
            "story_pressure_tokens": ["張り合い", "順番"],
            "forbidden_soft_landings": ["見ておく"],
            "recent_shape_history": ["answer_then_probe"],
            "recent_second_beat_history": ["press", "press"],
            "required_story_flavor_role": "pressure",
        },
    )

    assert result.action == "retry"
    voice_issue = next(issue for issue in result.issues if issue["issue_type"] == "voice_flat_reply")
    assert voice_issue["details"]["reply_quality_shape"] == "answer_then_probe"
    assert voice_issue["details"]["reply_story_flavor_role"] == "pressure"
    assert voice_issue["details"]["reply_story_flavor_seen"] is True
    assert voice_issue["details"]["reply_second_beat_reused"] is True
    assert voice_issue["details"]["reply_quality_contract_missed"] is True


def test_quality_guard_retries_when_story_quality_contract_is_missed_after_signal_visibility() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "星風 瑠奈、今決める。見せ場はまだ曖昧にしない。",
        msg_type="reply",
        target_char_name="星風 瑠奈",
        recent_dialogue_lines=["星風 瑠奈: 「それ、いつ決めるの？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "決め"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["見せ場"],
            "objective_anchor_tokens": [],
            "visibility_mode": "signal_first",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
        reply_story_quality_contract={
            "primary_shape": "answer_then_probe",
            "second_beat_mode": "press",
            "story_pressure_tokens": ["張り合い", "順番"],
            "forbidden_soft_landings": ["見ておく", "曖昧にしない"],
            "recent_shape_history": ["answer_then_probe"],
            "recent_second_beat_history": ["press", "press"],
            "required_story_flavor_role": "pressure",
            "story_pressure_cue_text": "張り合い / 順番 を曖昧に流さず、その場の争点として返す",
        },
    )

    assert result.action == "retry"
    voice_issue = next(issue for issue in result.issues if issue["issue_type"] == "voice_flat_reply")
    assert voice_issue["details"]["reply_story_quality_shape"] == "answer_then_probe"
    assert voice_issue["details"]["reply_story_flavor_role"] == "pressure"
    assert voice_issue["details"]["reply_story_flavor_seen"] is False
    assert voice_issue["details"]["reply_story_quality_contract_missed"] is True


def test_quality_guard_retries_when_residual_quality_contract_is_missed_after_signal_visibility() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "星風 瑠奈、今決める。見せ場はまだ曖昧にしない。",
        msg_type="reply",
        target_char_name="星風 瑠奈",
        recent_dialogue_lines=["星風 瑠奈: 「それ、いつ決めるの？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "決め"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["見せ場"],
            "objective_anchor_tokens": [],
            "visibility_mode": "signal_first",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
        reply_residual_quality_contract={
            "primary_shape": "answer_then_probe",
            "second_beat_mode": "press",
            "story_pressure_tokens": ["張り合い", "順番"],
            "forbidden_soft_landings": ["見ておく", "曖昧にしない"],
            "recent_shape_history": ["answer_then_probe"],
            "recent_second_beat_history": ["press", "press"],
            "required_story_flavor_role": "pressure",
            "story_pressure_cue_text": "張り合い / 順番 を曖昧に流さず、その場の争点として返す",
            "residual_block_threshold": "shape+second_beat+pressure_shift_or_story_flavor",
        },
    )

    assert result.action == "retry"
    voice_issue = next(issue for issue in result.issues if issue["issue_type"] == "voice_flat_reply")
    assert voice_issue["details"]["reply_residual_shape"] == "answer_then_probe"
    assert voice_issue["details"]["reply_residual_story_flavor_role"] == "pressure"
    assert voice_issue["details"]["reply_residual_story_flavor_seen"] is False
    assert voice_issue["details"]["reply_residual_contract_missed"] is True


def test_quality_guard_strips_think_tags() -> None:
    """<think>...</think> タグが除去されて発話本文のみ残ること。"""
    guard = _make_guard()

    result = guard.evaluate(
        "<think>これは内部思考です。返答を考えよう。</think>「わかった、やってみる。」",
        msg_type="reply",
    )

    assert "<think>" not in result.text
    assert "内部思考" not in result.text
    assert "やってみる" in result.text


def test_quality_guard_strips_thinking_tags_multiline() -> None:
    """複数行にわたる <thinking> ブロックが除去されること。"""
    guard = _make_guard()

    multiline_text = "<thinking>\nユーザーが求めるのは行動だ。\n具体的に答えよう。\n</thinking>「今すぐ動く。」"
    result = guard.evaluate(multiline_text, msg_type="reply")

    assert "<thinking>" not in result.text
    assert "ユーザーが求める" not in result.text
    assert "今すぐ動く" in result.text


def test_quality_guard_reply_focus_missing_with_novel_focus_text() -> None:
    """汎用フォールバック: 固定パターン外の reply_focus_text でも key トークンで照合できること。"""
    guard = _make_guard()

    # "音楽室" を含む focus_text に対して "音楽" を含む reply は accept
    result_accept = guard.evaluate(
        "音楽室に行こう。今すぐ動く。",
        msg_type="reply",
        reply_focus_text="音楽室で話すかどうか",
    )
    assert not any(issue["issue_type"] == "reply_focus_missing" for issue in result_accept.issues)

    # "音楽" を含まない reply は missing と判定
    result_missing = guard.evaluate(
        "別にどこでもいい。早く終わらせたい。",
        msg_type="reply",
        reply_focus_text="音楽室で話すかどうか",
    )
    assert any(issue["issue_type"] == "reply_focus_missing" for issue in result_missing.issues)


def test_quality_guard_voice_flat_あらっぽい_with_polite_tail() -> None:
    """荒っぽい voice anchor に対して丁寧な末尾は voice_flat_reply と判定されること。"""
    guard = _make_guard()

    result = guard.evaluate(
        "それでいい。よろしくお願いします。",
        msg_type="reply",
        voice_anchor_text="荒っぽい・粗暴な言動が持ち味",
    )

    assert any(issue["issue_type"] == "voice_flat_reply" for issue in result.issues)


def test_quality_guard_retries_flat_reply_when_recent_tail_is_reused() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "いつ決めるかだけ今ここで出す。順番はそのあとでいい。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「それ、いつ決めるんだ？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "決め"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_surface_contract={
            "surface_mode": "answer_first",
            "preferred_voice_cues": ["直球"],
            "must_vary_from_recent_self": True,
            "recent_self_endings": ["順番はそのあとでいい"],
        },
        voice_anchor_text="熱血直球で短く返す",
    )

    assert result.action == "retry"
    assert any(issue["issue_type"] == "voice_flat_reply" for issue in result.issues)


def test_quality_guard_allows_flat_reply_when_voice_cue_breaks_repetition() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "いつ決めるかだけ今ここで出す。なら先に出せよ。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「それ、いつ決めるんだ？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "決め"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_surface_contract={
            "surface_mode": "answer_first",
            "preferred_voice_cues": ["直球"],
            "must_vary_from_recent_self": True,
            "recent_self_endings": ["順番はそのあとでいい"],
        },
        voice_anchor_text="熱血直球で短く返す",
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "voice_flat_reply" for issue in result.issues)


def test_quality_guard_retries_when_signal_visible_reply_ends_in_soft_landing() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "上泉ソーマ、今決める。張り合いも順番もあとで片づける。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「それ、いつ決めるんだ？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "決め"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["先に"],
            "objective_anchor_tokens": [],
            "visibility_mode": "signal_first",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
        reply_variety_contract={
            "response_shape": "answer_then_press",
            "second_beat_mode": "press",
            "forbidden_recent_openings": [],
            "forbidden_recent_endings": [],
            "preferred_move_tokens": ["先に", "言って"],
        },
        reply_dramatic_contract={
            "move_mode": "counter",
            "allowed_secondary_modes": ["probe"],
            "required_move_tokens": ["先に", "言って", "出せ"],
            "must_change_pressure_in_second_sentence": True,
            "forbidden_soft_landings": ["あとで片づける", "曖昧にしない"],
        },
    )

    assert result.action == "retry"
    voice_issue = next(issue for issue in result.issues if issue["issue_type"] == "voice_flat_reply")
    assert voice_issue["details"]["reply_dramatic_move_mode"] == "counter"
    assert voice_issue["details"]["reply_dramatic_move_seen"] is False
    assert voice_issue["details"]["reply_soft_landing_used"] is True


def test_quality_guard_retries_when_dramatic_move_has_token_but_no_pressure_anchor() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "上泉ソーマ、まだ帰らない。張り合いなら先に言って。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「それ、いつ決めるんだ？」"],
        reply_focus_text="帰るかどうかの確認",
        reply_focus_contract={
            "focus_family": "yes_no",
            "required_tokens": ["帰", "残", "行"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["張り合い"],
            "objective_anchor_tokens": [],
            "visibility_mode": "signal_first",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
        reply_variety_contract={
            "response_shape": "answer_then_press",
            "second_beat_mode": "press",
            "forbidden_recent_openings": [],
            "forbidden_recent_endings": [],
            "preferred_move_tokens": ["先に", "言って"],
        },
        reply_dramatic_contract={
            "move_mode": "counter",
            "semantic_move_family": "counter",
            "pressure_anchor_tokens": ["順番"],
            "allowed_secondary_modes": ["probe"],
            "required_move_tokens": ["先に", "言って", "出せ"],
            "required_semantic_cues": ["違う", "先に", "まず"],
            "forbidden_semantic_drifts": ["見ておく", "そのあとでいい"],
            "must_change_pressure_in_second_sentence": True,
            "forbidden_soft_landings": ["順番はそのあとでいい"],
        },
    )

    assert result.action == "retry"
    voice_issue = next(issue for issue in result.issues if issue["issue_type"] == "voice_flat_reply")
    assert voice_issue["details"]["reply_dramatic_semantic_family"] == "counter"
    assert voice_issue["details"]["reply_dramatic_anchor_seen"] is False
    assert voice_issue["details"]["reply_dramatic_semantic_missed"] is True
    assert voice_issue["details"]["reply_dramatic_move_seen"] is False


def test_quality_guard_accepts_dramatic_move_when_semantic_family_and_anchor_match() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "上泉ソーマ、まだ帰らない。張り合いなら順番を先に出せ。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「それ、いつ決めるんだ？」"],
        reply_focus_text="帰るかどうかの確認",
        reply_focus_contract={
            "focus_family": "yes_no",
            "required_tokens": ["帰", "残", "行"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["張り合い"],
            "objective_anchor_tokens": [],
            "visibility_mode": "signal_first",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
        reply_variety_contract={
            "response_shape": "answer_then_press",
            "second_beat_mode": "press",
            "forbidden_recent_openings": [],
            "forbidden_recent_endings": [],
            "preferred_move_tokens": ["先に", "言って"],
        },
        reply_dramatic_contract={
            "move_mode": "counter",
            "semantic_move_family": "counter",
            "pressure_anchor_tokens": ["順番"],
            "allowed_secondary_modes": ["probe"],
            "required_move_tokens": ["先に", "言って", "出せ"],
            "required_semantic_cues": ["違う", "先に", "まず"],
            "forbidden_semantic_drifts": ["見ておく", "そのあとでいい"],
            "must_change_pressure_in_second_sentence": True,
            "forbidden_soft_landings": ["順番はそのあとでいい"],
        },
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "voice_flat_reply" for issue in result.issues)


def test_quality_guard_accepts_claim_move_seen_via_fixed_or_commit_language() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "上泉ソーマ、今決める。順番はここで固定する。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「それ、いつ決めるんだ？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "決め"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["張り合い"],
            "objective_anchor_tokens": ["順番"],
            "visibility_mode": "paired",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
        reply_dramatic_contract={
            "move_mode": "claim",
            "semantic_move_family": "claim",
            "pressure_anchor_tokens": ["順番"],
            "allowed_secondary_modes": ["probe"],
            "required_move_tokens": ["決める", "通す", "固定", "言い切る"],
            "required_semantic_cues": [],
            "forbidden_semantic_drifts": ["見ておく", "そのあとでいい"],
            "must_change_pressure_in_second_sentence": True,
            "forbidden_soft_landings": ["順番はそのあとでいい"],
        },
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "voice_flat_reply" for issue in result.issues)


def test_quality_guard_accepts_claim_move_seen_via_do_not_end_or_blur_language() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "上泉ソーマ、今のままでは終わらせない。見せ場も食い違いもここで曖昧にしない。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「このまま押し切る」"],
        reply_focus_text="相手の提案への返答",
        reply_focus_contract={
            "focus_family": "proposal",
            "required_tokens": ["なら", "じゃあ", "どう", "先に"],
            "required_focus_cues": ["なら", "じゃあ", "先に", "どう"],
            "focus_anchor_tokens": ["なら", "じゃあ"],
            "preferred_action": "propose",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["見せ場"],
            "objective_anchor_tokens": ["食い違い"],
            "visibility_mode": "paired",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
        reply_dramatic_contract={
            "move_mode": "claim",
            "semantic_move_family": "claim",
            "pressure_anchor_tokens": ["見せ場"],
            "allowed_secondary_modes": ["redirect"],
            "required_move_tokens": ["決める", "通す", "固定", "言い切る"],
            "required_semantic_cues": [],
            "forbidden_semantic_drifts": ["見ておく", "そのあとでいい"],
            "must_change_pressure_in_second_sentence": True,
            "forbidden_soft_landings": ["見ておく"],
        },
        reply_variety_contract={
            "response_shape": "answer_then_redirect",
            "second_beat_mode": "redirect",
            "forbidden_recent_openings": [],
            "forbidden_recent_endings": [],
            "preferred_move_tokens": ["じゃあ", "その前に"],
        },
        reply_shape_contract={
            "primary_shape": "answer_then_probe",
            "secondary_shape": "answer_then_counter",
            "must_shift_pressure_in_second_sentence": True,
            "forbidden_soft_landings": ["見ておく"],
            "recent_shape_history": ["answer_then_counter"],
            "story_pressure_tokens": ["見せ場", "食い違い"],
        },
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "signal_visibility_missing" for issue in result.issues)
    assert not any(issue["issue_type"] == "scene_objective_visibility_missing" for issue in result.issues)
    assert not any(issue["issue_type"] == "voice_flat_reply" for issue in result.issues)


def test_quality_guard_retries_when_bland_shape_is_reused_without_pressure_shift() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "上泉ソーマ、今決める。順番は見ておく。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「それ、いつ決めるんだ？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "決め"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["先に"],
            "objective_anchor_tokens": [],
            "visibility_mode": "signal_first",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
        reply_variety_contract={
            "response_shape": "answer_then_probe",
            "second_beat_mode": "press",
            "forbidden_recent_openings": [],
            "forbidden_recent_endings": [],
            "preferred_move_tokens": ["先に", "言って"],
        },
        reply_dramatic_contract={
            "move_mode": "probe",
            "allowed_secondary_modes": ["claim"],
            "required_move_tokens": ["なぜ", "どこで", "今"],
            "must_change_pressure_in_second_sentence": True,
            "forbidden_soft_landings": ["見ておく"],
        },
        reply_blandness_contract={
            "primary_shape": "answer_then_probe",
            "secondary_shape": "answer_then_condition",
            "must_shift_pressure_in_second_sentence": True,
            "forbidden_soft_landings": ["見ておく"],
            "recent_shape_history": ["answer_then_probe"],
        },
    )

    assert result.action == "retry"
    voice_issue = next(issue for issue in result.issues if issue["issue_type"] == "voice_flat_reply")
    assert voice_issue["details"]["reply_blandness_shape"] == "answer_then_probe"
    assert voice_issue["details"]["reply_blandness_shape_reused"] is True
    assert voice_issue["details"]["reply_pressure_shift_seen"] is False
    assert voice_issue["details"]["blandness_contract_missed"] is True


def test_quality_guard_normalizes_when_signal_visible_reply_has_move_but_is_still_soft() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "上泉ソーマ、今決める。張り合いは見せる、でも先に順番だけ出そう。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「それ、いつ決めるんだ？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "決め"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["先に"],
            "objective_anchor_tokens": [],
            "visibility_mode": "signal_first",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
        reply_variety_contract={
            "response_shape": "answer_then_press",
            "second_beat_mode": "press",
            "forbidden_recent_openings": [],
            "forbidden_recent_endings": [],
            "preferred_move_tokens": ["先に", "言って"],
        },
        reply_dramatic_contract={
            "move_mode": "counter",
            "allowed_secondary_modes": ["probe"],
            "required_move_tokens": ["先に", "言って", "出せ"],
            "must_change_pressure_in_second_sentence": True,
            "forbidden_soft_landings": ["あとで片づける", "曖昧にしない"],
        },
    )

    assert result.action == "normalize"
    voice_issue = next(issue for issue in result.issues if issue["issue_type"] == "voice_flat_reply")
    assert voice_issue["details"]["reply_dramatic_move_seen"] is True
    assert voice_issue["details"]["reply_soft_landing_used"] is False


def test_quality_guard_normalizes_when_story_flavor_is_weak_but_not_contract_missed() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "上泉ソーマ、今決める。先に言って。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「それ、いつ決めるんだ？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "決め"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["先に"],
            "objective_anchor_tokens": [],
            "visibility_mode": "signal_first",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
        reply_shape_contract={
            "primary_shape": "answer_then_probe",
            "secondary_shape": "answer_then_condition",
            "must_shift_pressure_in_second_sentence": True,
            "forbidden_soft_landings": ["見ておく"],
            "recent_shape_history": ["answer_then_counter"],
            "story_pressure_tokens": ["張り合い", "順番"],
        },
    )

    assert result.action == "normalize"
    voice_issue = next(issue for issue in result.issues if issue["issue_type"] == "voice_flat_reply")
    assert voice_issue["details"]["story_flavor_weak"] is True
    assert voice_issue["details"]["shape_contract_missed"] is False
    assert voice_issue["details"]["voice_flat_blocking"] is False
    assert voice_issue["details"]["voice_flat_residual_only"] is True


def test_quality_guard_skips_timing_story_flavor_residual_when_order_cue_and_probe_pressure_are_strong() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "今、順番は先に決めてから話す。主導権を誰が取るか先に言え。",
        msg_type="reply",
        target_char_name="ちゅるるん",
        recent_dialogue_lines=["ちゅるるん: 「それ、いつ決めるの？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "決め"],
            "required_focus_cues": ["今", "先に", "あとで", "いつ", "タイミング"],
            "focus_anchor_tokens": ["今", "先に", "あとで", "タイミング"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_dramatic_contract={
            "move_mode": "probe",
            "allowed_secondary_modes": ["claim"],
            "required_move_tokens": ["なぜ", "どこで", "今"],
            "must_change_pressure_in_second_sentence": True,
            "forbidden_soft_landings": ["見ておく"],
        },
        reply_shape_contract={
            "primary_shape": "answer_then_probe",
            "secondary_shape": "answer_then_condition",
            "must_shift_pressure_in_second_sentence": True,
            "forbidden_soft_landings": ["見ておく"],
            "recent_shape_history": ["answer_then_counter"],
            "story_pressure_tokens": ["見せ場", "流れ", "回収"],
        },
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "voice_flat_reply" for issue in result.issues)


def test_quality_guard_skips_basis_voice_flat_residual_when_condition_shape_has_pressure() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "夜風ユウマ、何の基準かを先に言ってください。流れをどこで切るか先に出して。",
        msg_type="reply",
        target_char_name="夜風ユウマ",
        recent_dialogue_lines=["夜風ユウマ: 「何を基準にするんだ？」"],
        reply_focus_text="判断基準の確認",
        reply_focus_contract={
            "focus_family": "basis",
            "required_tokens": ["基準", "何の", "どの", "判断"],
            "required_focus_cues": ["基準", "何の", "どの", "判断", "定義"],
            "focus_anchor_tokens": ["基準", "判断", "定義"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_shape_contract={
            "primary_shape": "answer_then_condition",
            "secondary_shape": "answer_then_probe",
            "must_shift_pressure_in_second_sentence": True,
            "forbidden_soft_landings": ["見ておく"],
            "recent_shape_history": ["answer_then_condition"],
            "story_pressure_tokens": ["流れ", "回収", "主導権"],
        },
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "voice_flat_reply" for issue in result.issues)


def test_quality_guard_normalizes_visibility_when_first_sentence_semantic_hit_is_strong() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "決める役はこっちが持つ。順番だけ先に通す。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「誰が決めるんだ？」"],
        reply_focus_text="誰が決めるかの確認",
        reply_focus_contract={
            "focus_family": "decision_owner",
            "required_tokens": ["誰", "決め", "役", "順番"],
            "required_focus_cues": ["誰が", "こっち", "私が", "お前が", "任せる", "決める役"],
            "focus_anchor_tokens": ["誰が", "こっち", "私が", "お前が", "決める役"],
            "forbidden_focus_drift_tokens": ["基準", "見せ場"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["見せ場"],
            "objective_anchor_tokens": ["主導権"],
            "visibility_mode": "paired",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
    )

    assert result.action == "normalize"
    signal_issue = next(issue for issue in result.issues if issue["issue_type"] == "signal_visibility_missing")
    objective_issue = next(
        issue for issue in result.issues if issue["issue_type"] == "scene_objective_visibility_missing"
    )
    assert signal_issue["severity"] == "info"
    assert objective_issue["severity"] == "info"
    assert signal_issue["details"]["visibility_blocking"] is False
    assert objective_issue["details"]["visibility_blocking"] is False
    assert signal_issue["details"]["reply_focus_first_sentence_semantic_hit"] is True


def test_quality_guard_suppresses_timing_objective_only_visibility_for_live_order_first_reply() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "今のタイミングで順番はわたしが決めるぜ。見せ場の主導権を誰が取るか、先に決め直すべきだぜ。",
        msg_type="reply",
        target_char_name="夜風ユウマ",
        recent_dialogue_lines=["夜風ユウマ: 「それ、いつ決めるんだ？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "決め"],
            "required_focus_cues": ["今", "先に", "あとで", "いつ", "タイミング"],
            "focus_anchor_tokens": ["今", "先に", "あとで", "タイミング"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": [],
            "objective_anchor_tokens": ["流れ", "回収"],
            "visibility_mode": "objective_first",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
        reply_shape_contract={
            "primary_shape": "answer_then_probe",
            "secondary_shape": "answer_then_condition",
            "must_shift_pressure_in_second_sentence": True,
            "forbidden_soft_landings": ["見ておく"],
            "recent_shape_history": ["answer_then_probe"],
            "story_pressure_tokens": ["見せ場", "主導権", "順番"],
        },
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "scene_objective_visibility_missing" for issue in result.issues)
    assert not any(issue["issue_type"] == "voice_flat_reply" for issue in result.issues)


def test_quality_guard_suppresses_timing_visibility_and_voice_flat_for_live_order_cut_reply() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "今、順番はここで区切るぜ。見せ場をどこに置くか、その順番を先に決めろよ。",
        msg_type="reply",
        target_char_name="夜風ユウマ",
        recent_dialogue_lines=["夜風ユウマ: 「それ、いつ決めるんだ？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "決め"],
            "required_focus_cues": ["今", "先に", "あとで", "いつ", "タイミング"],
            "focus_anchor_tokens": ["今", "先に", "あとで", "タイミング"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["流れ", "回収"],
            "objective_anchor_tokens": ["見せ場", "勝負"],
            "visibility_mode": "paired",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
        reply_shape_contract={
            "primary_shape": "answer_then_press",
            "secondary_shape": "answer_then_probe",
            "must_shift_pressure_in_second_sentence": True,
            "story_pressure_tokens": ["見せ場", "順番", "勝負"],
        },
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "signal_visibility_missing" for issue in result.issues)
    assert not any(issue["issue_type"] == "scene_objective_visibility_missing" for issue in result.issues)
    assert not any(issue["issue_type"] == "voice_flat_reply" for issue in result.issues)


def test_quality_guard_suppresses_decision_owner_objective_only_visibility_for_live_role_reply() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "ソーマの「役割」なんて、あたしがここで確定するよ。その主導権、あたしが取るから、順番はあたしが先に決めさせて。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「誰が決めるんだ？」"],
        reply_focus_text="誰が決めるかの確認",
        reply_focus_contract={
            "focus_family": "decision_owner",
            "required_tokens": ["誰", "決め", "役", "順番"],
            "required_focus_cues": ["誰が", "こっち", "私が", "お前が", "任せる", "決める役", "主導権"],
            "focus_anchor_tokens": ["誰が", "こっち", "私が", "お前が", "決める役", "主導権"],
            "forbidden_focus_drift_tokens": ["基準", "見せ場"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": [],
            "objective_anchor_tokens": ["主導権", "入れ替わり"],
            "visibility_mode": "objective_first",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "scene_objective_visibility_missing" for issue in result.issues)


def test_quality_guard_suppresses_decision_owner_signal_visibility_for_live_owner_reply() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "星風ルナ、誰が決めるか先に出す。流れも主導権もここで曖昧にしない。",
        msg_type="reply",
        target_char_name="星風ルナ",
        recent_dialogue_lines=["星風ルナ: 「誰が主導権を取るの？」"],
        reply_focus_text="誰が決めるかの確認",
        reply_focus_contract={
            "focus_family": "decision_owner",
            "required_tokens": ["誰", "決め", "役", "順番"],
            "required_focus_cues": ["誰が", "こっち", "私が", "お前が", "任せる", "決める役", "主導権"],
            "focus_anchor_tokens": ["誰が", "こっち", "私が", "お前が", "決める役", "主導権"],
            "forbidden_focus_drift_tokens": ["基準", "見せ場"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["流れ", "回収"],
            "objective_anchor_tokens": ["主導権", "入れ替わり"],
            "visibility_mode": "paired",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
        reply_shape_contract={
            "primary_shape": "answer_then_counter",
            "secondary_shape": "answer_then_press",
            "must_shift_pressure_in_second_sentence": True,
            "story_pressure_tokens": ["主導権", "流れ", "順番"],
        },
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "signal_visibility_missing" for issue in result.issues)
    assert not any(issue["issue_type"] == "voice_flat_reply" for issue in result.issues)


def test_quality_guard_suppresses_decision_owner_objective_visibility_for_live_flow_recovery_reply() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "ユウマ、本物なんて誰も決めるわけじゃない。この場の流れはここで強制的に回収する 先に、このセクションの主導権はうちが握る",
        msg_type="reply",
        target_char_name="夜風ユウマ",
        recent_dialogue_lines=["夜風ユウマ: 「誰が本物を決めるんだ？」"],
        reply_focus_text="誰が決めるかの確認",
        reply_focus_contract={
            "focus_family": "decision_owner",
            "required_tokens": ["誰", "決め", "役", "順番"],
            "required_focus_cues": ["誰が", "こっち", "私が", "お前が", "任せる", "決める役", "主導権"],
            "focus_anchor_tokens": ["誰が", "こっち", "私が", "お前が", "決める役", "主導権"],
            "forbidden_focus_drift_tokens": ["基準", "見せ場"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": [],
            "objective_anchor_tokens": ["流れ", "回収"],
            "visibility_mode": "objective_first",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
        reply_shape_contract={
            "primary_shape": "answer_then_counter",
            "secondary_shape": "answer_then_press",
            "must_shift_pressure_in_second_sentence": True,
            "story_pressure_tokens": ["主導権", "順番", "流れ"],
        },
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "scene_objective_visibility_missing" for issue in result.issues)


def test_quality_guard_suppresses_decision_owner_voice_flat_for_live_flow_recovery_reply() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "ユウマ、本物なんて誰も決めるわけじゃない。この場の流れはここで強制的に回収する 先に、このセクションの主導権はうちが握る",
        msg_type="reply",
        target_char_name="夜風ユウマ",
        recent_dialogue_lines=["夜風ユウマ: 「誰が本物を決めるんだ？」"],
        reply_focus_text="誰が決めるかの確認",
        reply_focus_contract={
            "focus_family": "decision_owner",
            "required_tokens": ["誰", "決め", "役", "順番"],
            "required_focus_cues": ["誰が", "こっち", "私が", "お前が", "任せる", "決める役", "主導権"],
            "focus_anchor_tokens": ["誰が", "こっち", "私が", "お前が", "決める役", "主導権"],
            "forbidden_focus_drift_tokens": ["基準", "見せ場"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": [],
            "objective_anchor_tokens": ["流れ", "回収"],
            "visibility_mode": "objective_first",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
        reply_shape_contract={
            "primary_shape": "answer_then_counter",
            "secondary_shape": "answer_then_press",
            "must_shift_pressure_in_second_sentence": True,
            "story_pressure_tokens": ["主導権", "順番", "流れ"],
        },
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "voice_flat_reply" for issue in result.issues)


def test_quality_guard_accepts_live_proposal_reply_with_counterproposal_in_second_sentence() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "ルナ、その「順番」の主張は、ただの場を停滞させるための口実に過ぎる。じゃあ、この回収の具体的な「順番」を、今すぐここで先に固定しないか。",
        msg_type="reply",
        target_char_name="星風ルナ",
        recent_dialogue_lines=["星風ルナ: 「その順番で進めるべきです」"],
        reply_focus_text="相手の提案への返答",
        reply_focus_contract={
            "focus_family": "proposal",
            "required_tokens": ["提案", "順番", "代案"],
            "required_focus_cues": ["じゃあ", "代わりに", "その前に", "順番を変える"],
            "focus_anchor_tokens": ["じゃあ", "代わりに", "その前に", "順番を変える"],
            "preferred_action": "propose",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": [],
            "objective_anchor_tokens": ["流れ", "回収"],
            "visibility_mode": "objective_first",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
        reply_shape_contract={
            "primary_shape": "answer_then_counter",
            "secondary_shape": "answer_then_redirect",
            "must_shift_pressure_in_second_sentence": True,
            "story_pressure_tokens": ["流れ", "回収", "順番"],
        },
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "reply_focus_missing" for issue in result.issues)


def test_quality_guard_suppresses_premise_objective_visibility_for_live_order_reset_reply() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "ルナの言う通り、始まりの音の前提は今ここで決めなきゃダメだろ。流れは、この場で順番を決め直す。",
        msg_type="reply",
        target_char_name="ルナ",
        recent_dialogue_lines=["ルナ: 「その前提、どこから決めるの？」"],
        reply_focus_text="前提条件の確認",
        reply_focus_contract={
            "focus_family": "premise",
            "required_tokens": ["前提", "条件", "その前"],
            "required_focus_cues": ["前提", "条件", "その前"],
            "focus_anchor_tokens": ["前提", "条件", "その前"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": [],
            "objective_anchor_tokens": ["流れ", "回収"],
            "visibility_mode": "objective_first",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
        reply_shape_contract={
            "primary_shape": "answer_then_condition",
            "secondary_shape": "answer_then_probe",
            "must_shift_pressure_in_second_sentence": True,
            "story_pressure_tokens": ["流れ", "順番"],
        },
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "scene_objective_visibility_missing" for issue in result.issues)


def test_quality_guard_accepts_premise_focus_via_followup_sentence_family_cue() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "ちゅるるん、始まりの音の前提は、私自身がまず先に決めたいです。"
        "その前提をどうするか、先に決めないと流れは回収できません。",
        msg_type="reply",
        target_char_name="ちゅるるん",
        recent_dialogue_lines=["ちゅるるん: 「その前提、どうするんだ？」"],
        reply_focus_text="相手の前提の確認",
        reply_focus_contract={
            "focus_family": "premise",
            "required_tokens": ["前提", "条件", "その前"],
            "required_focus_cues": ["前提", "条件", "その前"],
            "focus_anchor_tokens": ["前提", "条件", "その前"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
    )

    assert not any(issue["issue_type"] == "reply_focus_missing" for issue in result.issues)


def test_quality_guard_accepts_timing_focus_via_followup_sentence_cut_cue() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "先に、始まりの音の前提はうちが決める。順番は先に決めて、見せ場をどこで切るか決めろ。",
        msg_type="reply",
        target_char_name="ちゅるるん",
        recent_dialogue_lines=["ちゅるるん: 「それ、いつ決めるの？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "決め"],
            "required_focus_cues": ["今", "先に", "あとで", "いつ", "タイミング"],
            "focus_anchor_tokens": ["今", "先に", "あとで", "タイミング"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
    )

    assert not any(issue["issue_type"] == "reply_focus_missing" for issue in result.issues)


def test_quality_guard_skips_generic_redirect_story_flavor_when_scene_pressure_is_visible() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "ユウマ、主導権の話を外すのは楽な逃げだ。じゃあ、この見せ場の順番を今、先に固定しないか。",
        msg_type="reply",
        target_char_name="ユウマ",
        recent_dialogue_lines=["ユウマ: 「そのまま押し切るのか？」"],
        reply_shape_contract={
            "primary_shape": "answer_then_redirect",
            "secondary_shape": "answer_then_probe",
            "must_shift_pressure_in_second_sentence": True,
            "story_pressure_tokens": ["見せ場", "順番", "主導権"],
        },
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "voice_flat_reply" for issue in result.issues)


def test_quality_guard_target_name_tokens_include_trailing_given_name_segment() -> None:
    guard = _make_guard()

    tokens = guard._target_name_tokens("上泉ソーマ")

    assert "上泉ソーマ" in tokens
    assert "ソーマ" in tokens


def test_quality_guard_target_name_tokens_include_yukkuri_suffix_name() -> None:
    guard = _make_guard()

    tokens = guard._target_name_tokens("ゆっくり霊夢")

    assert "ゆっくり霊夢" in tokens
    assert "霊夢" in tokens


def test_quality_guard_suppresses_basis_objective_only_visibility_for_live_flow_cut_reply() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "夜風ユウマ、何で測るかを先に出す。流れをどこで切るか先に出して。",
        msg_type="reply",
        target_char_name="夜風ユウマ",
        recent_dialogue_lines=["夜風ユウマ: 「その基準、どこで決める？」"],
        reply_focus_text="判断基準の確認",
        reply_focus_contract={
            "focus_family": "basis",
            "required_tokens": ["基準", "何", "どの"],
            "required_focus_cues": ["基準", "何の", "どの", "何で", "どの基準", "判断基準"],
            "focus_anchor_tokens": ["基準", "何の", "どの", "どの基準", "判断基準"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": [],
            "objective_anchor_tokens": ["流れ", "回収"],
            "visibility_mode": "objective_first",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
    )

    assert not any(issue["issue_type"] == "scene_objective_visibility_missing" for issue in result.issues)


def test_quality_guard_suppresses_basis_flow_visibility_for_boundary_metric_reply() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "ソーマの言う「境界線」って、結局何で測るの？その判断基準を先に提示してくれないと、あたしは何も動けないよ。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「回収の境界線を先に示してほしい」"],
        reply_focus_text="判断基準の確認",
        reply_focus_contract={
            "focus_family": "basis",
            "required_tokens": ["基準", "何", "どの"],
            "required_focus_cues": ["基準", "何の", "どの", "何で", "どの基準", "判断基準"],
            "focus_anchor_tokens": ["基準", "何の", "どの", "どの基準", "判断基準"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": [],
            "objective_anchor_tokens": ["流れ", "回収"],
            "visibility_mode": "objective_first",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "scene_objective_visibility_missing" for issue in result.issues)


def test_quality_guard_accepts_decision_owner_focus_via_control_swap_cue() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "入れ替わりは先に俺が通す。その主導権をどう扱うつもりだ？",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「誰が決めるんだ？」"],
        reply_focus_text="誰が決めるかの確認",
        reply_focus_contract={
            "focus_family": "decision_owner",
            "required_tokens": ["誰", "決め", "役", "順番"],
            "required_focus_cues": ["誰が", "こっち", "私が", "お前が", "任せる", "決める役", "主導権"],
            "focus_anchor_tokens": ["誰が", "こっち", "私が", "お前が", "決める役", "主導権"],
            "forbidden_focus_drift_tokens": ["基準", "見せ場"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
    )

    assert not any(issue["issue_type"] == "reply_focus_missing" for issue in result.issues)


def test_quality_guard_accepts_decision_owner_focus_when_second_sentence_carries_owner_role() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "ソーマ、置きどころだけ先に決めるって、どこまでを「置きどころ」って言うの？"
        "ここで、誰がその「決める」という役割を担うか、先に決め直さないとね。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「誰が決めるんだ？」"],
        reply_focus_text="誰が決めるかの確認",
        reply_focus_contract={
            "focus_family": "decision_owner",
            "required_tokens": ["誰", "決め", "役", "順番"],
            "required_focus_cues": ["誰が", "こっち", "私が", "お前が", "任せる", "決める役", "主導権"],
            "focus_anchor_tokens": ["誰が", "こっち", "私が", "お前が", "決める役", "主導権"],
            "forbidden_focus_drift_tokens": ["基準", "見せ場"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
    )

    assert not any(issue["issue_type"] == "reply_focus_missing" for issue in result.issues)


def test_quality_guard_retries_when_shape_is_reused_without_story_pressure_token() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "上泉ソーマ、今決める。先に言って。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「それ、いつ決めるんだ？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "決め"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["張り合い"],
            "objective_anchor_tokens": ["順番"],
            "visibility_mode": "paired",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
        reply_shape_contract={
            "primary_shape": "answer_then_probe",
            "secondary_shape": "answer_then_condition",
            "must_shift_pressure_in_second_sentence": True,
            "forbidden_soft_landings": ["見ておく"],
            "recent_shape_history": ["answer_then_probe"],
            "story_pressure_tokens": ["張り合い", "順番"],
        },
    )

    assert result.action == "retry"
    voice_issue = next(issue for issue in result.issues if issue["issue_type"] == "voice_flat_reply")
    assert voice_issue["details"]["reply_shape_mode"] == "answer_then_probe"
    assert voice_issue["details"]["reply_shape_reused"] is True
    assert voice_issue["details"]["reply_story_pressure_seen"] is False
    assert voice_issue["details"]["story_flavor_weak"] is True


def test_quality_guard_accepts_when_shape_moves_and_story_pressure_is_visible() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "上泉ソーマ、今決める。順番を先に出すなら張り合いごと受ける。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「それ、いつ決めるんだ？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "決め"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["張り合い"],
            "objective_anchor_tokens": ["順番"],
            "visibility_mode": "paired",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
        reply_shape_contract={
            "primary_shape": "answer_then_probe",
            "secondary_shape": "answer_then_condition",
            "must_shift_pressure_in_second_sentence": True,
            "forbidden_soft_landings": ["見ておく"],
            "recent_shape_history": ["answer_then_counter"],
            "story_pressure_tokens": ["張り合い", "順番"],
        },
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "voice_flat_reply" for issue in result.issues)


def test_quality_guard_normalizes_when_only_dramatic_contract_is_soft_missed_with_pressure() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "上泉ソーマ、今決める。順番をここで固定する。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「それ、いつ決めるんだ？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "決め"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["張り合い"],
            "objective_anchor_tokens": ["順番"],
            "visibility_mode": "paired",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
        reply_dramatic_contract={
            "move_mode": "counter",
            "allowed_secondary_modes": ["probe"],
            "required_move_tokens": ["違う", "先に"],
            "must_change_pressure_in_second_sentence": True,
            "forbidden_soft_landings": ["見ておく"],
        },
        reply_shape_contract={
            "primary_shape": "answer_then_counter",
            "secondary_shape": "answer_then_probe",
            "must_shift_pressure_in_second_sentence": True,
            "forbidden_soft_landings": ["見ておく"],
            "recent_shape_history": ["answer_then_probe"],
            "story_pressure_tokens": ["張り合い", "順番"],
        },
    )

    assert result.action == "normalize"
    voice_issue = next(issue for issue in result.issues if issue["issue_type"] == "voice_flat_reply")
    assert voice_issue["details"]["dramatic_contract_missed"] is True
    assert voice_issue["details"]["reply_dramatic_anchor_seen"] is True
    assert voice_issue["details"]["reply_story_pressure_seen"] is True
    assert voice_issue["details"]["voice_flat_blocking"] is False


def test_quality_guard_voice_flat_helper_treats_shape_and_dramatic_residual_as_nonblocking() -> None:
    assert QualityGuard._is_blocking_voice_flat_details(
        {
            "dramatic_contract_missed": True,
            "shape_contract_missed": True,
            "reply_pressure_shift_seen": True,
            "reply_story_pressure_seen": True,
            "reply_dramatic_move_seen": True,
            "reply_dramatic_anchor_seen": False,
        }
    ) is False


def test_quality_guard_voice_flat_helper_treats_reused_second_beat_with_pressure_as_nonblocking() -> None:
    assert QualityGuard._is_blocking_voice_flat_details(
        {
            "variety_contract_missed": True,
            "flat_issue_family": "reused_second_beat",
            "reply_pressure_shift_seen": True,
            "reply_story_pressure_seen": True,
            "reply_dramatic_move_seen": True,
        }
    ) is False


def test_quality_guard_voice_flat_helper_treats_basis_reused_second_beat_with_pressure_as_nonblocking() -> None:
    assert QualityGuard._is_blocking_voice_flat_details(
        {
            "variety_contract_missed": True,
            "dramatic_contract_missed": True,
            "flat_issue_family": "reused_second_beat",
            "reply_focus_family": "basis",
            "reply_shape_mode": "answer_then_condition",
            "reply_pressure_shift_seen": True,
            "reply_story_pressure_seen": True,
            "reply_dramatic_anchor_seen": True,
            "reply_second_beat_reused": True,
            "reply_soft_landing_used": False,
        }
    ) is False


def test_quality_guard_voice_flat_helper_treats_basis_story_flavor_with_pressure_as_nonblocking() -> None:
    assert QualityGuard._is_blocking_voice_flat_details(
        {
            "variety_contract_missed": True,
            "dramatic_contract_missed": True,
            "flat_issue_family": "story_flavor_weak",
            "reply_focus_family": "basis",
            "reply_shape_mode": "answer_then_condition",
            "reply_pressure_shift_seen": True,
            "reply_story_pressure_seen": True,
            "reply_second_beat_reused": True,
            "reply_soft_landing_used": False,
        }
    ) is False


def test_quality_guard_voice_flat_helper_treats_timing_reused_second_beat_with_pressure_as_nonblocking() -> None:
    assert QualityGuard._is_blocking_voice_flat_details(
        {
            "variety_contract_missed": True,
            "dramatic_contract_missed": True,
            "flat_issue_family": "reused_second_beat",
            "reply_focus_family": "timing",
            "reply_shape_mode": "answer_then_probe",
            "reply_pressure_shift_seen": True,
            "reply_story_pressure_seen": True,
            "reply_second_beat_reused": True,
            "reply_soft_landing_used": False,
        }
    ) is False


def test_quality_guard_voice_flat_helper_treats_basis_story_flavor_residual_as_nonblocking() -> None:
    assert QualityGuard._is_blocking_voice_flat_details(
        {
            "shape_contract_missed": True,
            "reply_story_quality_contract_missed": True,
            "flat_issue_family": "story_flavor_weak",
            "reply_focus_family": "basis",
            "reply_pressure_shift_seen": True,
            "reply_dramatic_move_seen": True,
        }
    ) is False


def test_quality_guard_voice_flat_helper_treats_basis_condition_shape_dominance_as_nonblocking() -> None:
    assert QualityGuard._is_blocking_voice_flat_details(
        {
            "shape_contract_missed": True,
            "flat_issue_family": "shape_dominance",
            "reply_focus_family": "basis",
            "reply_shape_mode": "answer_then_condition",
            "story_flavor_weak": True,
            "reply_soft_landing_used": False,
        }
    ) is False


def test_quality_guard_voice_flat_helper_treats_basis_condition_shape_dominance_with_dramatic_pressure_as_nonblocking() -> None:
    assert QualityGuard._is_blocking_voice_flat_details(
        {
            "dramatic_contract_missed": True,
            "blandness_contract_missed": True,
            "shape_contract_missed": True,
            "flat_issue_family": "shape_dominance",
            "reply_focus_family": "basis",
            "reply_shape_mode": "answer_then_condition",
            "story_flavor_weak": True,
            "reply_soft_landing_used": False,
        }
    ) is False


def test_quality_guard_voice_flat_helper_treats_basis_condition_shape_dominance_with_anchor_and_story_pressure_as_nonblocking() -> None:
    assert QualityGuard._is_blocking_voice_flat_details(
        {
            "dramatic_contract_missed": True,
            "blandness_contract_missed": True,
            "shape_contract_missed": True,
            "flat_issue_family": "shape_dominance",
            "reply_focus_family": "basis",
            "reply_shape_mode": "answer_then_condition",
            "reply_dramatic_anchor_seen": True,
            "reply_story_pressure_seen": True,
            "reply_soft_landing_used": False,
            "story_flavor_weak": False,
        }
    ) is False


def test_quality_guard_voice_flat_helper_treats_basis_condition_shape_dominance_with_quality_and_reuse_flags_as_nonblocking() -> None:
    assert QualityGuard._is_blocking_voice_flat_details(
        {
            "variety_contract_missed": True,
            "dramatic_contract_missed": True,
            "blandness_contract_missed": True,
            "shape_contract_missed": True,
            "reply_quality_contract_missed": True,
            "reply_story_quality_contract_missed": True,
            "reply_residual_contract_missed": True,
            "flat_issue_family": "shape_dominance",
            "reply_focus_family": "basis",
            "reply_shape_mode": "answer_then_condition",
            "reply_dramatic_anchor_seen": True,
            "reply_story_pressure_seen": True,
            "reply_second_beat_reused": True,
            "reply_soft_landing_used": False,
            "story_flavor_weak": False,
        }
    ) is False


def test_quality_guard_voice_flat_helper_treats_basis_condition_story_flavor_with_dramatic_miss_as_nonblocking() -> None:
    assert QualityGuard._is_blocking_voice_flat_details(
        {
            "dramatic_contract_missed": True,
            "flat_issue_family": "story_flavor_weak",
            "reply_focus_family": "basis",
            "reply_shape_mode": "answer_then_condition",
            "story_flavor_weak": True,
            "reply_soft_landing_used": False,
        }
    ) is False


def test_quality_guard_voice_flat_helper_treats_probe_story_flavor_with_pressure_shift_as_nonblocking() -> None:
    assert QualityGuard._is_blocking_voice_flat_details(
        {
            "dramatic_contract_missed": True,
            "flat_issue_family": "story_flavor_weak",
            "reply_shape_mode": "answer_then_probe",
            "reply_pressure_shift_seen": True,
            "reply_soft_landing_used": False,
        }
    ) is False


def test_quality_guard_pressure_shift_accepts_basis_control_tokens_for_condition_shape() -> None:
    assert QualityGuard._reply_pressure_shift_satisfied(
        "主導権はここで固定する。",
        shape="answer_then_condition",
    ) is True


def test_quality_guard_voice_flat_helper_treats_basis_voice_flat_with_anchor_and_story_pressure_as_nonblocking() -> None:
    assert QualityGuard._is_blocking_voice_flat_details(
        {
            "flat_issue_family": "voice_flat",
            "reply_focus_family": "basis",
            "reply_shape_mode": "answer_then_condition",
            "reply_dramatic_anchor_seen": True,
            "reply_story_pressure_seen": True,
            "reply_soft_landing_used": False,
        }
    ) is False


def test_quality_guard_skips_decision_owner_counter_residual_when_control_pressure_is_visible() -> None:
    assert QualityGuard._should_skip_residual_voice_flat_issue(
        {
            "flat_issue_family": "voice_flat",
            "reply_focus_family": "decision_owner",
            "reply_shape_mode": "answer_then_counter",
            "reply_story_pressure_seen": True,
            "reply_soft_landing_used": False,
        },
        focus_diagnostics={"reply_focus_first_sentence_semantic_hit": True},
        text="主導権は俺が先に確定させる。その入れ替わりをどうするつもりだ？",
    ) is True


def test_quality_guard_voice_flat_helper_treats_decision_owner_reused_opening_with_counter_pressure_as_nonblocking() -> None:
    assert QualityGuard._is_blocking_voice_flat_details(
        {
            "variety_contract_missed": True,
            "dramatic_contract_missed": True,
            "flat_issue_family": "reused_opening",
            "reply_focus_family": "decision_owner",
            "reply_shape_mode": "answer_then_counter",
            "reply_shape_reused_recently": True,
            "reply_pressure_shift_seen": True,
            "reply_story_pressure_seen": True,
            "reply_dramatic_anchor_seen": True,
            "reply_soft_landing_used": False,
        }
    ) is False


def test_quality_guard_voice_flat_helper_treats_decision_owner_story_flavor_with_counter_pressure_as_nonblocking() -> None:
    assert QualityGuard._is_blocking_voice_flat_details(
        {
            "dramatic_contract_missed": True,
            "flat_issue_family": "story_flavor_weak",
            "reply_focus_family": "decision_owner",
            "reply_shape_mode": "answer_then_counter",
            "reply_pressure_shift_seen": True,
            "reply_soft_landing_used": False,
        }
    ) is False


def test_quality_guard_voice_flat_helper_treats_timing_press_cut_line_as_nonblocking() -> None:
    assert QualityGuard._should_skip_residual_voice_flat_issue(
        {
            "flat_issue_family": "story_flavor_weak",
            "reply_focus_family": "timing",
            "reply_shape_mode": "answer_then_press",
            "reply_soft_landing_used": False,
        },
        focus_diagnostics={"reply_focus_first_sentence_semantic_hit": True},
        text="ソーマ、どこで切るか提案してもいいとか、勝手に決めんな。先に、この場の主導権はうちが握る。",
    ) is True


def test_quality_guard_voice_flat_helper_treats_generic_claim_redirect_with_flow_pressure_as_nonblocking() -> None:
    assert QualityGuard._should_skip_residual_voice_flat_issue(
        {
            "flat_issue_family": "voice_flat",
            "reply_focus_family": None,
            "reply_shape_mode": "answer_then_redirect",
            "reply_soft_landing_used": False,
        },
        focus_diagnostics={"reply_focus_first_sentence_semantic_hit": False},
        text=(
            "ちゅるるんが「主導権を握る」と言うなら、その主導権の定義を明確にしていただけますか。"
            "単に発言の順序を決めるだけでは、本当の意味での流れの主導権とは言えませんよ。"
        ),
    ) is True


def test_quality_guard_voice_flat_helper_treats_generic_probe_line_decision_as_nonblocking() -> None:
    assert QualityGuard._should_skip_residual_voice_flat_issue(
        {
            "flat_issue_family": "story_flavor_weak",
            "reply_focus_family": None,
            "reply_shape_mode": "answer_then_probe",
            "reply_pressure_shift_seen": True,
            "reply_soft_landing_used": False,
        },
        focus_diagnostics={"reply_focus_first_sentence_semantic_hit": False},
        text="先に、主導権の順番なんて何も決めてないでしょ？あたしが最初に引くラインを決めろ、だろ。",
    ) is True


def test_quality_guard_voice_flat_helper_treats_decision_owner_reused_second_beat_with_counter_pressure_as_nonblocking() -> None:
    assert QualityGuard._is_blocking_voice_flat_details(
        {
            "variety_contract_missed": True,
            "dramatic_contract_missed": True,
            "flat_issue_family": "story_flavor_weak",
            "reply_focus_family": "decision_owner",
            "reply_shape_mode": "answer_then_counter",
            "reply_pressure_shift_seen": True,
            "reply_second_beat_reused": True,
            "reply_soft_landing_used": False,
        }
    ) is False


def test_quality_guard_skips_basis_condition_story_flavor_residual_when_pressure_tokens_are_explicit() -> None:
    assert QualityGuard._should_skip_residual_voice_flat_issue(
        {
            "flat_issue_family": "story_flavor_weak",
            "reply_focus_family": "basis",
            "reply_shape_mode": "answer_then_condition",
            "reply_pressure_shift_seen": True,
            "reply_soft_landing_used": False,
        },
        focus_diagnostics={"reply_focus_first_sentence_semantic_hit": True},
        text="基準の話は先に決めろ。置きどころを決められないなら、この議論は終わらせる。",
    ) is True


def test_quality_guard_does_not_raise_signal_override_for_live_showdown_line_reply() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "ミリティア、誰が決めるかなんて、うちが今ここで決める。見せ場を出すなら、先にこの勝負のラインを決めろよ。",
        msg_type="reply",
        dominant_signal_text="見せ場を張ると場が動きやすい。",
    )

    assert not any(issue["issue_type"] == "signal_override" for issue in result.issues)


def test_quality_guard_does_not_raise_signal_override_for_live_recovery_flow_reply() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "今のうち、ルナの定義の話なんてどうでもいい、先に見せ場の順番を決めろってことでしょ。その順番を固定してから、あたしが勝負の場所を出すわ。",
        msg_type="reply",
        dominant_signal_text="ここで回収に寄せると流れが前に進みやすい。",
    )

    assert not any(issue["issue_type"] == "signal_override" for issue in result.issues)


def test_quality_guard_accepts_live_basis_turn_148_line_without_voice_flat_issue() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "ルナ、判断基準の話は後だ。先に「見せ場」の場所を固定しろ。",
        msg_type="reply",
        target_char_name="星風 瑠奈",
        recent_dialogue_lines=["星風 瑠奈: 「その見せ場、誰の基準で決めるんだ？」"],
        reply_focus_text="判断基準の確認",
        reply_focus_contract={
            "focus_family": "basis",
            "required_tokens": ["基準", "何", "どの"],
            "required_focus_cues": ["基準", "何の", "どの", "何で", "どの基準", "判断基準"],
            "focus_anchor_tokens": ["基準", "何の", "どの", "どの基準", "判断基準"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["見せ場"],
            "objective_anchor_tokens": ["見せ場", "勝負"],
            "visibility_mode": "paired",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
        reply_shape_contract={
            "primary_shape": "answer_then_condition",
            "secondary_shape": "answer_then_probe",
            "must_shift_pressure_in_second_sentence": True,
            "forbidden_soft_landings": ["見ておく"],
            "recent_shape_history": ["answer_then_condition"],
            "story_pressure_tokens": ["見せ場", "勝負", "順番", "ライン", "場所"],
        },
        reply_dramatic_contract={
            "move_mode": "condition",
            "allowed_secondary_modes": ["probe"],
            "required_move_tokens": ["先に", "固定", "確定"],
            "must_change_pressure_in_second_sentence": True,
            "forbidden_soft_landings": ["見ておく"],
        },
        voice_anchor_text="挑発混じりに短く押す",
    )

    assert result.action == "accept"
    assert not any(issue["issue_type"] == "voice_flat_reply" for issue in result.issues)


def test_quality_guard_accepts_live_generic_conflict_probe_without_focus_miss() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "夜風ユウマ、まだ整理できてない。食い違いのどこを潰すか先に出して。",
        msg_type="reply",
        target_char_name="夜風ユウマ",
        recent_dialogue_lines=["夜風ユウマ: 「その食い違い、どこから片づける？」"],
        reply_focus_text="食い違っている論点",
    )

    assert not any(issue["issue_type"] == "reply_focus_missing" for issue in result.issues)


def test_quality_guard_accepts_live_proposal_order_reset_reply_without_voice_flat_issue() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "ユウマが主導権を握るって言うなら、まずは「見せ場」の順番から決め直すべきじゃない？"
        "その主導権、あたしが先に取るよ、さあ。",
        msg_type="reply",
        target_char_name="夜風ユウマ",
        recent_dialogue_lines=["夜風ユウマ: 「主導権なら俺が握る」"],
        reply_focus_text="相手の提案への返答",
        reply_focus_contract={
            "focus_family": "proposal",
            "required_tokens": ["なら", "じゃあ", "先に", "どう"],
            "required_focus_cues": ["なら", "じゃあ", "先に", "どう"],
            "focus_anchor_tokens": ["なら", "じゃあ", "その前に", "代わりに"],
            "preferred_action": "propose",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["見せ場"],
            "objective_anchor_tokens": ["食い違い"],
            "visibility_mode": "paired",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
        reply_variety_contract={
            "response_shape": "answer_then_redirect",
            "second_beat_mode": "redirect",
            "preferred_move_tokens": ["じゃあ", "その前に", "まず", "代わりに"],
        },
        reply_shape_contract={
            "primary_shape": "answer_then_counter",
            "secondary_shape": "answer_then_redirect",
            "must_shift_pressure_in_second_sentence": True,
            "story_pressure_tokens": ["見せ場", "主導権", "順番"],
        },
        reply_dramatic_contract={
            "move_mode": "counter",
            "semantic_move_family": "counter",
            "pressure_anchor_tokens": ["見せ場", "主導権"],
            "required_move_tokens": ["先に", "決め直", "取る"],
            "must_change_pressure_in_second_sentence": True,
        },
        voice_anchor_text="挑発混じりに短く押す",
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "voice_flat_reply" for issue in result.issues)
    assert not any(issue["issue_type"] == "scene_objective_drift" for issue in result.issues)


def test_quality_guard_accepts_live_proposal_order_change_reply_without_focus_miss() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "夜風ユウマ、じゃあ順番を変える。主導権はこっちが取る",
        msg_type="reply",
        target_char_name="夜風ユウマ",
        recent_dialogue_lines=["夜風ユウマ: 「その主導権、誰が取る？」"],
        reply_focus_text="相手の提案への返答",
        reply_focus_contract={
            "focus_family": "proposal",
            "required_tokens": ["なら", "じゃあ", "先に", "どう"],
            "required_focus_cues": ["なら", "じゃあ", "先に", "どう"],
            "focus_anchor_tokens": ["なら", "じゃあ", "その前に", "代わりに"],
            "preferred_action": "propose",
            "must_answer_in_first_sentence": True,
        },
    )

    assert not any(issue["issue_type"] == "reply_focus_missing" for issue in result.issues)


def test_quality_guard_accepts_live_basis_fixed_order_reply_without_voice_flat_issue() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "基準は、君たちが『盛り上がる』という曖昧な感情だけじゃない。そのあと、見せ場の順番を先に固定しないか。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「その基準、何で決めるんだ？」"],
        reply_focus_text="判断基準の確認",
        reply_focus_contract={
            "focus_family": "basis",
            "required_tokens": ["基準", "何", "どの"],
            "required_focus_cues": ["基準", "何の", "どの", "何で", "どの基準", "判断基準"],
            "focus_anchor_tokens": ["基準", "何の", "どの", "どの基準", "判断基準"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_shape_contract={
            "primary_shape": "answer_then_condition",
            "secondary_shape": "answer_then_probe",
            "must_shift_pressure_in_second_sentence": True,
            "story_pressure_tokens": ["見せ場", "順番", "場所"],
        },
        voice_anchor_text="静かな確認で短く返す",
    )

    assert not any(issue["issue_type"] == "voice_flat_reply" for issue in result.issues)


def test_quality_guard_accepts_live_basis_role_split_reply_without_voice_flat_issue() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "判断基準は、感情ではなく明確な役割分けで測るべきです。"
        "主導権を巡るこの議論は、順番ではなく、誰が次の「見せ場」を担うかという形で確定させるべきではないでしょうか。",
        msg_type="reply",
        target_char_name="星風ルナ",
        recent_dialogue_lines=["星風ルナ: 「その基準、何で決めるの？」"],
        reply_focus_text="判断基準の確認",
        reply_focus_contract={
            "focus_family": "basis",
            "required_tokens": ["基準", "何", "どの"],
            "required_focus_cues": ["基準", "何の", "どの", "何で", "どの基準", "判断基準"],
            "focus_anchor_tokens": ["基準", "何の", "どの", "どの基準", "判断基準"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_shape_contract={
            "primary_shape": "answer_then_condition",
            "secondary_shape": "answer_then_probe",
            "must_shift_pressure_in_second_sentence": True,
            "story_pressure_tokens": ["主導権", "見せ場", "順番", "役割"],
        },
        voice_anchor_text="短く断定して返す",
    )

    assert not any(issue["issue_type"] == "voice_flat_reply" for issue in result.issues)


def test_quality_guard_accepts_live_generic_line_decision_reply_without_voice_flat_issue() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "先に、主導権の順番なんて何も決めてないでしょ？あたしが最初に引くラインを決めろ、だろ。",
        msg_type="reply",
        voice_anchor_text="静かな確認で短く返す",
        reply_shape_contract={
            "primary_shape": "answer_then_probe",
            "secondary_shape": "answer_then_redirect",
            "must_shift_pressure_in_second_sentence": True,
            "story_pressure_tokens": ["主導権", "順番", "ライン"],
        },
    )

    assert not any(issue["issue_type"] == "voice_flat_reply" for issue in result.issues)


def test_quality_guard_accepts_live_decision_owner_claim_reply_without_visibility_issue() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "霊夢、境界線を決めるのは俺がやる。主導権は今、この場で出せ。",
        msg_type="reply",
        target_char_name="霊夢",
        recent_dialogue_lines=["霊夢: 「その境界線、誰が決めるの？」"],
        reply_focus_text="誰が決めるかの確認",
        reply_focus_contract={
            "focus_family": "decision_owner",
            "required_tokens": ["誰", "決め", "役", "順番"],
            "required_focus_cues": ["誰が", "こっち", "私が", "お前が", "任せる", "決める役", "主導権"],
            "focus_anchor_tokens": ["誰が", "こっち", "私が", "お前が", "決める役", "主導権"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": [],
            "objective_anchor_tokens": ["主導権", "入れ替わり"],
            "visibility_mode": "objective_first",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
        voice_anchor_text="熱血直球で短く返す",
    )

    assert not any(issue["issue_type"] == "scene_objective_visibility_missing" for issue in result.issues)


def test_quality_guard_suppresses_basis_signal_checks_for_live_conflict_resolution_reply() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "判断基準は「食い違いの解消」だぜ。その食い違いなら、まずどの部分を潰すか先に決めてから話を進めるべきだぜ。",
        msg_type="reply",
        target_char_name="ゆっくり魔理沙",
        recent_dialogue_lines=["ゆっくり魔理沙: 「その判断基準、何だ？」"],
        dominant_signal_text="見せ場を張ると場が動きやすい。",
        reply_focus_text="判断基準の確認",
        reply_focus_contract={
            "focus_family": "basis",
            "required_tokens": ["基準", "何", "どの"],
            "required_focus_cues": ["基準", "何の", "どの", "判断基準"],
            "focus_anchor_tokens": ["基準", "何の", "どの", "判断基準"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["主導権"],
            "objective_anchor_tokens": ["食い違い"],
            "visibility_mode": "paired",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "signal_visibility_missing" for issue in result.issues)
    assert not any(issue["issue_type"] == "signal_override" for issue in result.issues)


def test_quality_guard_accepts_live_timing_flow_cut_reply_without_focus_miss() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "ミリティアが範囲を決めるなら、その範囲で流れをどこに収束させるか、先に決めましょう。"
        "まず、この「食い違い」の具体的な境界線から、流れを回収するのが筋でしょう。",
        msg_type="reply",
        target_char_name="ミリティア",
        recent_dialogue_lines=["ミリティア: 「その範囲、どこで切るのよ？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "決め"],
            "required_focus_cues": ["今", "先に", "あとで", "始まる前", "動く前"],
            "focus_anchor_tokens": ["今", "先に", "始まる前", "動く前"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": [],
            "objective_anchor_tokens": ["流れ", "回収"],
            "visibility_mode": "objective_first",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
    )

    assert not any(issue["issue_type"] == "reply_focus_missing" for issue in result.issues)
    assert not any(issue["issue_type"] == "scene_objective_visibility_missing" for issue in result.issues)


def test_quality_guard_accepts_live_timing_flow_cut_probe_without_voice_flat() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "上泉ソーマ、いつ決めるか今ここで出せ。流れをどこで切るか先に出して",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「いつ決めるんだ？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "決め"],
            "required_focus_cues": ["今", "先に", "あとで", "いつ", "タイミング"],
            "focus_anchor_tokens": ["今", "先に", "あとで", "タイミング"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": [],
            "objective_anchor_tokens": ["流れ", "回収"],
            "visibility_mode": "objective_first",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
        reply_variety_contract={
            "response_shape": "answer_then_press",
            "second_beat_mode": "press",
            "forbidden_recent_second_beats": ["press"],
        },
        reply_shape_contract={
            "primary_shape": "answer_then_probe",
            "secondary_shape": "answer_then_press",
            "must_shift_pressure_in_second_sentence": True,
            "story_pressure_tokens": ["見せ場", "勝負"],
        },
        reply_dramatic_contract={
            "move_mode": "probe",
            "semantic_move_family": "probe",
            "pressure_anchor_tokens": ["見せ場", "勝負"],
            "required_move_tokens": ["どこで", "切る", "出して"],
            "must_change_pressure_in_second_sentence": True,
        },
        voice_anchor_text="熱血直球で短く返す",
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "voice_flat_reply" for issue in result.issues)


def test_quality_guard_suppresses_live_timing_paired_visibility_for_line_cut_reply() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "ルナ、先に決めるってのは、ただの逃げじゃねえか。どこを区切るか、先にうちがライン引いてやるから、お前はそれを壊す準備しとけ。",
        msg_type="reply",
        target_char_name="ルナ",
        recent_dialogue_lines=["ルナ: 「いつ決めるの？」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "決め"],
            "required_focus_cues": ["今", "先に", "あとで", "いつ", "タイミング"],
            "focus_anchor_tokens": ["今", "先に", "あとで", "タイミング"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": ["主導権"],
            "objective_anchor_tokens": ["主導権", "入れ替わり"],
            "visibility_mode": "paired",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
        reply_shape_contract={
            "primary_shape": "answer_then_probe",
            "secondary_shape": "answer_then_press",
            "must_shift_pressure_in_second_sentence": True,
            "story_pressure_tokens": ["主導権", "順番", "ライン"],
        },
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "signal_visibility_missing" for issue in result.issues)


def test_quality_guard_suppresses_live_timing_scene_objective_for_flow_range_cut_reply() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "ちゅるるん、その「流れ」をここで決めるのか。そもそも、その流れの範囲をどこで区切るのか、先に決めろよ。",
        msg_type="reply",
        target_char_name="ちゅるるん",
        recent_dialogue_lines=["ちゅるるん: 「じゃあ、この流れをここで決める。」"],
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "決め"],
            "required_focus_cues": ["今", "先に", "あとで", "いつ", "タイミング"],
            "focus_anchor_tokens": ["今", "先に", "あとで", "タイミング"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": [],
            "objective_anchor_tokens": ["見せ場", "勝負"],
            "visibility_mode": "objective_first",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "scene_objective_visibility_missing" for issue in result.issues)


def test_quality_guard_accepts_live_decision_owner_redefinition_reply_without_voice_flat() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "霊夢、その主導権を誰が決めるのか、そこを曖昧にするな。"
        "この場の「誰が主導権を持つか」という前提条件を先に再定義しないと話にならないだろ。",
        msg_type="reply",
        target_char_name="霊夢",
        recent_dialogue_lines=["霊夢: 「その主導権、誰が決めるの？」"],
        reply_focus_text="誰が決めるかの確認",
        reply_focus_contract={
            "focus_family": "decision_owner",
            "required_tokens": ["誰", "決め", "役", "順番"],
            "required_focus_cues": ["誰が", "こっち", "私が", "お前が", "任せる", "決める役", "主導権"],
            "focus_anchor_tokens": ["誰が", "こっち", "私が", "お前が", "決める役", "主導権"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_shape_contract={
            "primary_shape": "answer_then_counter",
            "secondary_shape": "answer_then_press",
            "must_shift_pressure_in_second_sentence": True,
            "story_pressure_tokens": ["主導権", "順番", "前提"],
        },
        reply_dramatic_contract={
            "move_mode": "condition",
            "semantic_move_family": "condition",
            "pressure_anchor_tokens": ["主導権", "前提"],
            "required_move_tokens": ["先に", "再定義", "決める"],
            "must_change_pressure_in_second_sentence": True,
        },
        voice_anchor_text="短く断定して返す",
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "voice_flat_reply" for issue in result.issues)


def test_quality_guard_suppresses_generic_objective_only_visibility_for_live_flow_recovery_reply() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "その範囲で流れをどこに収束させるか、先に決めましょう。"
        "まず、この「食い違い」の具体的な境界線から、流れを回収するのが筋でしょう。",
        msg_type="reply",
        reply_signal_contract={
            "signal_anchor_tokens": [],
            "objective_anchor_tokens": ["流れ", "回収"],
            "visibility_mode": "objective_first",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
    )

    assert not any(issue["issue_type"] == "scene_objective_visibility_missing" for issue in result.issues)


def test_quality_guard_accepts_live_timing_conflict_boundary_reply_without_drift() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "先に、食い違いの境界線から流れることはしない。どこで切るか、その「食い違い」の主導権は誰が握るのか、先に決め直せ。",
        msg_type="reply",
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 「その食い違い、どこで切る？」"],
        scene_objective_text="この場の争点: 食い違いをほどく",
        reply_focus_text="いつ動くかの確認",
        reply_focus_contract={
            "focus_family": "timing",
            "required_tokens": ["いつ", "今", "決め"],
            "required_focus_cues": ["今", "先に", "あとで", "始まる前", "動く前"],
            "focus_anchor_tokens": ["今", "先に", "始まる前", "動く前"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": [],
            "objective_anchor_tokens": ["食い違い"],
            "visibility_mode": "objective_first",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
    )

    assert not any(issue["issue_type"] == "scene_objective_drift" for issue in result.issues)
    assert not any(issue["issue_type"] == "scene_objective_visibility_missing" for issue in result.issues)


def test_quality_guard_accepts_live_decision_owner_flow_cut_reply_without_visibility_issue() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "ゆっくり霊夢、誰が決めるか先に言え。流れなら切り替える場所を先に決める。",
        msg_type="reply",
        target_char_name="ゆっくり霊夢",
        recent_dialogue_lines=["ゆっくり霊夢: 「その主導権、誰が決めるの？」"],
        scene_objective_text="この場の争点: 流れを回収する",
        reply_focus_text="誰が決めるかの確認",
        reply_focus_contract={
            "focus_family": "decision_owner",
            "required_tokens": ["誰", "決め", "役", "順番"],
            "required_focus_cues": ["誰が", "こっち", "私が", "お前が", "任せる", "決める役", "主導権"],
            "focus_anchor_tokens": ["誰が", "こっち", "私が", "お前が", "決める役", "主導権"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": [],
            "objective_anchor_tokens": ["流れ", "回収"],
            "visibility_mode": "objective_first",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
        reply_shape_contract={
            "primary_shape": "answer_then_counter",
            "secondary_shape": "answer_then_press",
            "must_shift_pressure_in_second_sentence": True,
            "story_pressure_tokens": ["主導権", "流れ", "場所"],
        },
        voice_anchor_text="熱血直球で短く返す",
    )

    assert not any(issue["issue_type"] == "scene_objective_visibility_missing" for issue in result.issues)


def test_quality_guard_accepts_live_decision_owner_boundary_reply_without_direct_reaction_soft() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "霊夢、その境界線は誰が決めるんだ。この場で、流れをどこで切るか、その境界線を先に決め直そうぜ。",
        msg_type="reply",
        target_char_name="ゆっくり霊夢",
        recent_dialogue_lines=["ゆっくり霊夢: 「その境界線、誰が決めるの？」"],
        reply_focus_text="誰が決めるかの確認",
        reply_focus_contract={
            "focus_family": "decision_owner",
            "required_tokens": ["誰", "決め", "役", "順番"],
            "required_focus_cues": ["誰が", "こっち", "私が", "お前が", "任せる", "決める役", "主導権"],
            "focus_anchor_tokens": ["誰が", "こっち", "私が", "お前が", "決める役", "主導権"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": [],
            "objective_anchor_tokens": ["主導権", "入れ替わり"],
            "visibility_mode": "objective_first",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
    )

    assert not any(issue["issue_type"] == "reply_direct_reaction_soft" for issue in result.issues)


def test_quality_guard_suppresses_decision_owner_flow_visibility_for_scene_order_redefinition() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "ミリティア、誰が決めるかって話か。その「見せ場の順番」の定義自体を、俺が今ここで決め直させてもらう。",
        msg_type="reply",
        target_char_name="ミリティア",
        recent_dialogue_lines=["ミリティア: 「その順番、誰が決めるの？」"],
        reply_focus_text="誰が決めるかの確認",
        reply_focus_contract={
            "focus_family": "decision_owner",
            "required_tokens": ["誰", "決め", "役", "順番"],
            "required_focus_cues": ["誰が", "こっち", "私が", "お前が", "任せる", "決める役", "主導権"],
            "focus_anchor_tokens": ["誰が", "こっち", "私が", "お前が", "決める役", "主導権"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": [],
            "objective_anchor_tokens": ["流れ", "回収"],
            "visibility_mode": "objective_first",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
    )

    assert result.action in {"accept", "normalize"}
    assert not any(issue["issue_type"] == "scene_objective_visibility_missing" for issue in result.issues)


def test_quality_guard_suppresses_decision_owner_objective_only_visibility_for_boundary_reply() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "ゆっくり魔理沙、誰が決めるかだけ先に出そう。主導権を出すなら順番だけ先に通す",
        msg_type="reply",
        target_char_name="ゆっくり魔理沙",
        recent_dialogue_lines=["ゆっくり魔理沙: 「その主導権、誰が決めるの？」"],
        reply_focus_text="誰が決めるかの確認",
        reply_focus_contract={
            "focus_family": "decision_owner",
            "required_tokens": ["誰", "決め", "役", "順番"],
            "required_focus_cues": ["誰が", "こっち", "私が", "お前が", "任せる", "決める役", "主導権"],
            "focus_anchor_tokens": ["誰が", "こっち", "私が", "お前が", "決める役", "主導権"],
            "preferred_action": "confirm",
            "must_answer_in_first_sentence": True,
        },
        reply_signal_contract={
            "signal_anchor_tokens": [],
            "objective_anchor_tokens": ["主導権", "入れ替わり"],
            "visibility_mode": "objective_first",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
    )

    assert not any(issue["issue_type"] == "scene_objective_visibility_missing" for issue in result.issues)


def test_quality_guard_does_not_raise_signal_override_for_live_conflict_boundary_recovery_reply() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "ちゅるるん、流さないって言うなら、まずこの食い違いの境界線から正面から決め直すのが筋だぜ。"
        "それとも、結局曖昧なまま、誰かが回収するのを待ってるだけなのか？",
        msg_type="reply",
        dominant_signal_text="ここで回収に寄せると流れが前に進みやすい。",
        scene_objective_text="この場の争点: 流れを回収する",
    )

    assert not any(issue["issue_type"] == "signal_override" for issue in result.issues)
    assert not any(issue["issue_type"] == "scene_objective_drift" for issue in result.issues)


def test_quality_guard_does_not_raise_signal_override_for_live_do_not_blur_conflict_reply() -> None:
    guard = _make_guard()

    result = guard.evaluate(
        "上泉ソーマ、そこは流さない。流れも食い違いもここで曖昧にしない",
        msg_type="reply",
        dominant_signal_text="ここで回収に寄せると流れが前に進みやすい。",
        scene_objective_text="この場の争点: 食い違いをほどく",
        reply_signal_contract={
            "signal_anchor_tokens": ["流れ", "回収"],
            "objective_anchor_tokens": ["食い違い"],
            "visibility_mode": "paired",
            "must_surface_in_first_two_sentences": True,
            "forbidden_generic_replacements": ["その話"],
        },
    )

    assert not any(issue["issue_type"] == "signal_override" for issue in result.issues)

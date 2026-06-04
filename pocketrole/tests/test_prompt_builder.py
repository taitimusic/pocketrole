"""Tests for engine/prompt_builder.py — 14 test cases."""
import pytest
from engine.story_style import get_story_style_profile, get_story_style_profile_by_mode
from engine.prompt_builder import (
    CharacterContext,
    build_group_prompt,
    build_initiation_prompt,
    build_inner_thought_prompt,
    build_monologue_prompt,
    build_reply_prompt,
    build_voiced_group_prompt,
    build_voiced_reply_prompt,
)

LLM_VISIBLE_META_TERMS = (
    "判断基準",
    "論点",
    "争点",
    "押しどころ",
    "主導権",
    "食い違い",
    "residual",
    "pressure",
)


def make_ctx(**overrides) -> CharacterContext:
    """デフォルト値付きの CharacterContext を生成する。"""
    defaults = {
        "char_id": "yokaze_yuuma",
        "name_ja": "夜風 勇真",
        "personality_core": "クールで無口だが内心は熱い",
        "first_person": "俺",
        "tone": "短く端的に話す",
        "speech_examples": ["「別に。」", "「…そうか。」"],
        "never_say": ["です", "ます"],
        "current_goal": "バンドを組む",
        "current_worry": "弦が切れた",
        "sim_datetime": "2025-04-15T10:30",
        "current_place_name": "音楽室",
    }
    defaults.update(overrides)
    return CharacterContext(**defaults)


def test_character_context_creation():
    ctx = make_ctx()
    assert ctx.char_id == "yokaze_yuuma"
    assert ctx.name_ja == "夜風 勇真"
    assert ctx.first_person == "俺"
    assert ctx.speech_examples == ["「別に。」", "「…そうか。」"]
    assert ctx.never_say == ["です", "ます"]
    assert ctx.sim_datetime == "2025-04-15T10:30"
    assert ctx.current_place_name == "音楽室"


def test_build_monologue_returns_tuple():
    ctx = make_ctx()
    result = build_monologue_prompt(ctx)
    assert isinstance(result, tuple)
    assert len(result) == 2


def test_both_prompts_are_strings():
    ctx = make_ctx()
    system_prompt, user_prompt = build_monologue_prompt(ctx)
    assert isinstance(system_prompt, str)
    assert isinstance(user_prompt, str)


def test_system_prompt_keeps_180_chars_as_direct_limit() -> None:
    ctx = make_ctx(max_utterance_chars=180)
    system_prompt, _ = build_monologue_prompt(ctx)

    assert "発話の文字数は最大 180 字以内" in system_prompt
    assert "通常は180字以内" not in system_prompt


def test_system_prompt_treats_320_chars_as_exceptional_upper_bound() -> None:
    ctx = make_ctx(max_utterance_chars=320)
    system_prompt, _ = build_monologue_prompt(ctx)

    assert "通常は180字以内" in system_prompt
    assert "必要な場合だけ最大 320 字" in system_prompt
    assert "発言は最大 2 文以内" in system_prompt


def test_system_prompt_contains_name():
    ctx = make_ctx()
    system_prompt, _ = build_monologue_prompt(ctx)
    assert ctx.name_ja in system_prompt


def test_system_prompt_contains_personality():
    ctx = make_ctx()
    system_prompt, _ = build_monologue_prompt(ctx)
    assert ctx.personality_core in system_prompt


def test_system_prompt_contains_first_person():
    ctx = make_ctx()
    system_prompt, _ = build_monologue_prompt(ctx)
    assert ctx.first_person in system_prompt


def test_system_prompt_contains_tone():
    ctx = make_ctx()
    system_prompt, _ = build_monologue_prompt(ctx)
    assert ctx.tone in system_prompt


def test_system_prompt_contains_examples():
    ctx = make_ctx()
    system_prompt, _ = build_monologue_prompt(ctx)
    for example in ctx.speech_examples:
        assert example in system_prompt


def test_system_prompt_contains_goal():
    ctx = make_ctx()
    system_prompt, _ = build_monologue_prompt(ctx)
    assert ctx.current_goal in system_prompt


def test_system_prompt_contains_worry():
    ctx = make_ctx()
    system_prompt, _ = build_monologue_prompt(ctx)
    assert ctx.current_worry in system_prompt


def test_system_prompt_contains_never_say():
    ctx = make_ctx(never_say=["です", "ます"])
    system_prompt, _ = build_monologue_prompt(ctx)
    assert "絶対に使わない" in system_prompt
    assert "です" in system_prompt
    assert "ます" in system_prompt


def test_system_prompt_no_never_say_when_empty():
    ctx = make_ctx(never_say=[])
    system_prompt, _ = build_monologue_prompt(ctx)
    assert "絶対に使わない" not in system_prompt


def test_system_prompt_forbids_markdown_style_decorations():
    ctx = make_ctx()
    system_prompt, _ = build_monologue_prompt(ctx)
    assert "Markdown 記法" in system_prompt
    assert "**" in system_prompt
    assert "箇条書き" in system_prompt
    assert "発言本文だけ" in system_prompt


def test_user_prompt_contains_datetime():
    ctx = make_ctx()
    _, user_prompt = build_monologue_prompt(ctx)
    assert ctx.sim_datetime in user_prompt


def test_user_prompt_contains_place():
    ctx = make_ctx()
    _, user_prompt = build_monologue_prompt(ctx)
    assert ctx.current_place_name in user_prompt


def test_reply_prompt_requires_name_or_point_in_opening() -> None:
    ctx = make_ctx(target_char_name="星風 瑠奈", recent_dialogue_lines=["星風 瑠奈: まだ帰らないのか？"])
    _, user_prompt = build_reply_prompt(ctx)
    assert "星風 瑠奈" in user_prompt  # 相手の名前が状況に含まれる


def test_reply_prompt_does_not_prescribe_backend_focus_vocabulary() -> None:
    ctx = make_ctx(
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: その見せ場、誰の基準で決めるんだ？"],
        reply_focus_text="判断基準の確認",
    )
    _, user_prompt = build_reply_prompt(ctx)
    # メタ語彙を含む reply_focus は行ごと出力されない (None → "今返す一点: ..." 行なし)
    assert "今返す一点: " not in user_prompt  # "今返す一点" は固定指示文に含まれるが "今返す一点: " は focus 行
    assert "判断基準" not in user_prompt


def test_reply_prompt_does_not_expose_second_sentence_guidance() -> None:
    """新設計: second sentence guidance は user_prompt に出ない。指示密度削減の一部。"""
    ctx = make_ctx(
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: その見せ場、誰の基準で決めるんだ？"],
        reply_focus_text="判断基準の確認",
    )
    _, user_prompt = build_reply_prompt(ctx)
    assert "相手への問いかけ・感情・具体的な行動の提案" not in user_prompt
    assert "順番 / 見せ場 / 場所" not in user_prompt
    # 引用ブロックは維持
    assert "その見せ場、誰の基準で決めるんだ？" in user_prompt


def test_reply_prompt_does_not_expose_timing_focus_in_user_prompt() -> None:
    """新設計: reply_focus_text (timing 系) は user_prompt に出ない。"""
    ctx = make_ctx(
        target_char_name="星風 瑠奈",
        recent_dialogue_lines=["星風 瑠奈: それ、いつ決めるの？"],
        reply_focus_text="いつ動くかの確認",
    )
    _, user_prompt = build_reply_prompt(ctx)
    assert "今返す一点: いつ動くかの確認" not in user_prompt
    assert "範囲をどこで切るか" not in user_prompt
    # 引用ブロックは維持
    assert "それ、いつ決めるの？" in user_prompt


def test_reply_prompt_does_not_expose_timing_second_sentence_guidance() -> None:
    """新設計: timing 系の second sentence guidance は user_prompt に出ない。"""
    ctx = make_ctx(
        target_char_name="ちゅるるん",
        recent_dialogue_lines=["ちゅるるん: それ、いつ決めるの？"],
        reply_focus_text="いつ動くかの確認",
    )
    _, user_prompt = build_reply_prompt(ctx)
    assert "主導権 / 順番 / 流れをどこで切るか" not in user_prompt
    # 引用ブロックは維持
    assert "それ、いつ決めるの？" in user_prompt


def test_reply_prompt_scrubs_conflict_backend_focus_text() -> None:
    ctx = make_ctx(
        target_char_name="夜風 ユウマ",
        recent_dialogue_lines=["夜風 ユウマ: その話、どこから片づける？"],
        reply_focus_text="食い違っている論点",
    )
    _, user_prompt = build_reply_prompt(ctx)
    # メタ語彙を含む reply_focus は行ごと出力されない ("今返す一点: " focus 行なし)
    assert "今返す一点: " not in user_prompt
    assert "食い違い" not in user_prompt
    assert "論点" not in user_prompt


def test_reply_prompt_does_not_expose_decision_owner_second_sentence_guidance() -> None:
    """新設計: decision-owner 系の second sentence guidance は user_prompt に出ない。"""
    ctx = make_ctx(
        target_char_name="霊夢",
        recent_dialogue_lines=["霊夢: その話、誰が決めるの？"],
        reply_focus_text="誰が決めるかの確認",
    )
    _, user_prompt = build_reply_prompt(ctx)
    assert "相手への問いかけ・感情・具体的な行動の提案" not in user_prompt
    assert "主導権" not in user_prompt


def test_reply_prompt_does_not_expose_proposal_second_sentence_guidance() -> None:
    """新設計: proposal 系の second sentence guidance は user_prompt に出ない。"""
    ctx = make_ctx(
        target_char_name="夜風 ユウマ",
        recent_dialogue_lines=["夜風 ユウマ: それ、誰が引き受ける？"],
        reply_focus_text="相手の提案への返答",
    )
    _, user_prompt = build_reply_prompt(ctx)
    assert "主導権" not in user_prompt


# ============================================================
# Phase 2 テスト（14 件）
# ============================================================

def make_ctx_p2(**overrides) -> CharacterContext:
    """Phase 2 フィールドを含む CharacterContext を生成する。"""
    kwargs: dict = {
        "emotions": {"stress": 0.7, "motivation": 0.8, "loneliness": 0.3, "excitement": 0.6},
        "current_expression": "happy",
        "move_reason": "屋上から音楽室へ移動した",
        "anomaly": {"label": "深夜の音楽室", "drama_potential": "high", "suggested_reason": "test"},
        "same_place_char_names": ["星風 瑠奈", "ミリティア"],
        "secret_hint": "あなたの心の奥には、まだ誰にも話していないことがある。",
        "memory_texts": ["昨日、瑠奈と屋上で話した。"],
    }
    kwargs.update(overrides)
    return make_ctx(**kwargs)


# --- CharacterContext デフォルト値 ---

def test_phase2_ctx_default_emotions_empty():
    """`emotions` のデフォルトは空 dict"""
    ctx = make_ctx()
    assert ctx.emotions == {}


def test_phase2_ctx_default_expression_neutral():
    """`current_expression` のデフォルトは 'neutral'"""
    ctx = make_ctx()
    assert ctx.current_expression == "neutral"


def test_phase2_ctx_default_no_move_reason():
    """`move_reason` のデフォルトは None"""
    ctx = make_ctx()
    assert ctx.move_reason is None


def test_phase2_ctx_default_no_secret_hint():
    """`secret_hint` のデフォルトは None"""
    ctx = make_ctx()
    assert ctx.secret_hint is None


# --- system_prompt: 感情 ---

def test_emotion_text_in_system_prompt():
    """emotions 設定時 → system_prompt に表情テキストを含む"""
    ctx = make_ctx_p2(current_expression="happy")
    system_prompt, _ = build_monologue_prompt(ctx)
    assert "嬉しい気分" in system_prompt


def test_emotion_section_omitted_when_empty():
    """`emotions={}` → system_prompt に「現在の感情」セクションを含まない"""
    ctx = make_ctx(emotions={})
    system_prompt, _ = build_monologue_prompt(ctx)
    assert "現在の感情" not in system_prompt


# --- system_prompt: 記憶 ---

def test_memory_in_system_prompt():
    """`memory_texts` あり → system_prompt に記憶テキストを含む"""
    ctx = make_ctx_p2()
    system_prompt, _ = build_monologue_prompt(ctx)
    assert "昨日、瑠奈と屋上で話した。" in system_prompt


def test_memory_section_omitted_when_empty():
    """`memory_texts=[]` → system_prompt に「最近の記憶」セクションを含まない"""
    ctx = make_ctx(memory_texts=[])
    system_prompt, _ = build_monologue_prompt(ctx)
    assert "最近の記憶" not in system_prompt


# --- system_prompt: 秘密ヒント ---

def test_secret_hint_in_system_prompt():
    """`secret_hint` あり → system_prompt にその文字列を含む"""
    ctx = make_ctx_p2()
    system_prompt, _ = build_monologue_prompt(ctx)
    assert ctx.secret_hint in system_prompt


def test_secret_hint_omitted_when_none():
    """`secret_hint=None` → system_prompt に「心の内」セクションを含まない"""
    ctx = make_ctx(secret_hint=None)
    system_prompt, _ = build_monologue_prompt(ctx)
    assert "心の内" not in system_prompt


# --- user_prompt: 同一場所キャラ ---

def test_same_place_chars_in_user_prompt():
    """`same_place_char_names` あり → user_prompt にキャラ名を含む"""
    ctx = make_ctx_p2()
    _, user_prompt = build_monologue_prompt(ctx)
    assert "星風 瑠奈" in user_prompt


def test_same_place_chars_omitted_when_empty():
    """`same_place_char_names=[]` → user_prompt に「周囲にいる人」を含まない"""
    ctx = make_ctx(same_place_char_names=[])
    _, user_prompt = build_monologue_prompt(ctx)
    assert "周囲にいる人" not in user_prompt


# --- user_prompt: 移動理由 ---

def test_move_reason_in_user_prompt():
    """`move_reason` あり → user_prompt に移動理由テキストを含む"""
    ctx = make_ctx_p2()
    _, user_prompt = build_monologue_prompt(ctx)
    assert ctx.move_reason in user_prompt


# --- user_prompt: 異常 ---

def test_anomaly_label_in_user_prompt():
    """`anomaly` あり → user_prompt に anomaly["label"] を含む"""
    ctx = make_ctx_p2()
    _, user_prompt = build_monologue_prompt(ctx)
    assert ctx.anomaly["label"] in user_prompt


def test_light_banter_profile_adds_compact_concrete_rules() -> None:
    """drama モード profile 時、system_prompt に mystery_drama 向けルールが入る。"""
    ctx = make_ctx(
        style_profile=get_story_style_profile_by_mode("drama"),
    )
    system_prompt, _ = build_monologue_prompt(ctx)
    assert "具体的な相手・物・行動" in system_prompt
    assert "最大 2 文以内" in system_prompt
    assert "詩的な独白や抽象的な感傷は避けてください" in system_prompt


def test_pre_turn_scene_script_injected_in_system_prompt() -> None:
    """pre_turn_scene_script が CharacterContext にあると system_prompt に演技指導として挿入される。"""
    ctx = make_ctx(
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 黙っていてくれ"],
        pre_turn_scene_script="廊下に薄暗い蛍光灯の光が落ちている。ソーマが窓際に立っている。",
    )
    system_prompt, _ = build_reply_prompt(ctx)
    assert "演技指導" in system_prompt
    assert "台詞には出さない" in system_prompt
    assert "廊下に薄暗い蛍光灯" in system_prompt


def test_pre_turn_scene_script_absent_when_none() -> None:
    """pre_turn_scene_script が None のとき演技指導セクションは挿入されない。"""
    ctx = make_ctx(
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 黙っていてくれ"],
    )
    system_prompt, _ = build_reply_prompt(ctx)
    assert "演技指導ここまで" not in system_prompt


def test_character_directive_card_is_injected_without_full_scene_script_dump() -> None:
    """structured directive card は専用ブロックで注入され、全文スクリプト依存を避ける。"""
    ctx = make_ctx(
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 黙っていてくれ"],
        pre_turn_scene_script="〔シーン〕 長い演技指導全文",
        character_directive_text=(
            "役割: 情報を守る側\n"
            "次の動き: 手帳を守る\n"
            "発話タスク: 見せたくない理由をぼかして返す"
        ),
    )
    system_prompt, _ = build_reply_prompt(ctx)

    assert "【監督からあなたへの実行カード】" in system_prompt
    assert "見せたくない理由をぼかして返す" in system_prompt
    assert "〔シーン〕 長い演技指導全文" not in system_prompt


@pytest.mark.parametrize(
    "builder",
    [build_reply_prompt, build_group_prompt, build_initiation_prompt, build_monologue_prompt],
)
def test_scene_script_brief_is_injected_for_all_prompt_types(builder) -> None:
    ctx = make_ctx(
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 手帳を見せろ"],
        scene_frame_text="蛍光灯が点滅し、手帳の角だけが青白く見える。",
        scene_turn_goal_text="手帳を守る理由を会話の中心に寄せる。",
        scene_turn_shift_text="ちゅるるんが手帳へ一歩近づく。",
        scene_banned_surface_patterns=["残った所を言葉にする", "流れをどうする"],
    )

    system_prompt, _ = builder(ctx)

    assert "【このターンの場面設計】" in system_prompt
    assert "蛍光灯が点滅" in system_prompt
    assert "手帳を守る理由" in system_prompt
    assert "ちゅるるんが手帳へ一歩近づく" in system_prompt
    assert "残った所を言葉にする" not in system_prompt
    assert "流れをどうする" not in system_prompt
    assert "台詞にそのまま出さない" in system_prompt


def test_reply_reaction_unit_avoids_reinjecting_fixed_confirmation_phrase() -> None:
    ctx = make_ctx(
        target_char_name="上泉ソーマ",
        recent_dialogue_lines=["上泉ソーマ: 黙っていてくれ"],
    )
    _, user_prompt = build_reply_prompt(ctx)

    assert "目の前の一点を確かめる" not in user_prompt


def test_monologue_prompt_requires_concrete_context_and_short_length() -> None:
    """monologue prompt は具体物/行動と短さを明示する。"""
    ctx = make_ctx()
    system_prompt, user_prompt = build_monologue_prompt(ctx)
    assert "最大 2 文以内" in system_prompt
    assert "具体的な物・相手・行動" in system_prompt or "具体的な相手・物・行動" in system_prompt
    assert "独り言" in user_prompt


def test_monologue_prompt_surfaces_scene_objective_and_signal_guidance() -> None:
    ctx = make_ctx(
        scene_objective_text="この場の争点: 張り合いを前に出す",
        dominant_signal_text="張り合いに触れると場が動く",
    )

    _, user_prompt = build_monologue_prompt(ctx)

    assert "張り合いを前に出す" in user_prompt
    assert "張り合いに触れると場が動く" in user_prompt
    assert "この場面の流れ" in user_prompt
    assert "争点" not in user_prompt


def test_reply_prompt_contains_latest_partner_message():
    """reply prompt は相手の直前発言を含み、返答指示になる。"""
    ctx = make_ctx(
        target_char_name="星風 瑠奈",
        recent_dialogue_lines=["星風 瑠奈: 「……まだ帰らないの？」"],
    )
    _, user_prompt = build_reply_prompt(ctx)
    assert "星風 瑠奈" in user_prompt
    assert "……まだ帰らないの？" in user_prompt
    assert "出してください" in user_prompt  # 新設計: 心を踏まえて口に出す指示


def test_reply_prompt_does_not_expose_scaffold_labels() -> None:
    """新設計: scaffold ラベル（拾う一点/立場/理由/次の一手/返答の骨格）は user_prompt に出ない。

    これらのラベルは LLM がそのまま台詞にエコーする「カンペ語彙」の温床だったため撤去。
    代わりに inner_thought (2段目) で内的状態を自由に生成させる。
    """
    ctx = make_ctx(
        target_char_name="星風 瑠奈",
        recent_dialogue_lines=["星風 瑠奈: 「灯里先輩の鍵がまだ残っています」"],
        reaction_frame="警戒しながら相手の言葉を受け止めている",
        speaker_objective="鍵の出所を確かめる",
        relationship_frame="複雑な間柄",
    )

    _, user_prompt = build_reply_prompt(ctx)

    assert "【返答の骨格" not in user_prompt
    assert "拾う一点:" not in user_prompt
    assert "立場: " not in user_prompt
    assert "理由: " not in user_prompt
    assert "次の一手:" not in user_prompt
    # 引用ブロックは維持
    assert "灯里先輩の鍵がまだ残っています" in user_prompt
    assert "星風 瑠奈 がこう言った:" in user_prompt


def test_reply_prompt_requires_direct_first_sentence_reaction():
    """reply prompt は最初の1文で直接反応するよう明示する。"""
    ctx = make_ctx(
        target_char_name="星風 瑠奈",
        recent_dialogue_lines=["星風 瑠奈: 「……まだ帰らないの？」"],
    )
    _, user_prompt = build_reply_prompt(ctx)
    assert "星風 瑠奈 がこう言った:" in user_prompt  # 引用ブロックが出力されている
    assert "「……まだ帰らないの？」" in user_prompt


def test_reply_prompt_includes_target_excerpt_and_voice_anchor() -> None:
    """reply prompt は引用ブロックと voice anchor を明示する（reply_focus_text は user_prompt に出ない）。"""
    ctx = make_ctx(
        target_char_name="星風 瑠奈",
        recent_dialogue_lines=["星風 瑠奈: 「まだ帰らないの？」"],
        target_last_utterance_excerpt="まだ帰らないの？",
        reply_focus_text="帰るかどうかの確認",
        voice_anchor_text="静かな確認で短く返す",
    )

    system_prompt, user_prompt = build_reply_prompt(ctx)

    assert "返し方の芯" in system_prompt
    assert "静かな確認で短く返す" in system_prompt
    assert "星風 瑠奈 がこう言った:" in user_prompt  # 引用ブロック形式
    assert "まだ帰らないの？" in user_prompt
    # 新設計: reply_focus_text は user_prompt に出力されない（inner_thought 生成側で活用）
    assert "今返す一点" not in user_prompt
    assert "帰るかどうかの確認" not in user_prompt


def test_reply_prompt_does_not_expose_focus_text_in_user_prompt() -> None:
    """新設計: reply_focus_text は user_prompt に出力されない。"""
    ctx = make_ctx(
        target_char_name="上泉ソーマ",
        target_last_utterance_excerpt="残りを言えば済む話だ",
        reply_focus_text="誰が決めるかの確認",
    )

    _, user_prompt = build_reply_prompt(ctx)

    assert "今返す一点" not in user_prompt
    assert "誰が決めるかの確認" not in user_prompt


def test_reply_prompt_does_not_expose_sentence_level_guidance() -> None:
    """新設計: sentence-level guidance (1文目は…/2文目は…) は user_prompt に出ない。

    過剰な指示密度がカンペ症状を引き起こしていたため、指示文を 3 行以下に簡素化した。
    """
    ctx = make_ctx(
        target_char_name="上泉ソーマ",
        target_last_utterance_excerpt="残りを言えば済む話だ",
        reply_focus_text="誰が決めるかの確認",
    )

    _, user_prompt = build_reply_prompt(ctx)

    assert "自分の気持ちや立場に正直な反応" not in user_prompt
    assert "1文目は" not in user_prompt
    assert "肩書き" not in user_prompt
    assert "権利" not in user_prompt


def test_reply_prompt_does_not_expose_timing_guidance() -> None:
    """新設計: timing-based focus guidance は user_prompt に出ない。"""
    ctx = make_ctx(
        target_char_name="ゆっくり魔理沙",
        target_last_utterance_excerpt="それ、いつ決めるんだ？",
        reply_focus_text="いつ動くかの確認",
    )

    _, user_prompt = build_reply_prompt(ctx)

    assert "1文目は自分の気持ちや立場に正直な反応" not in user_prompt
    assert "流れをどこで切るか" not in user_prompt


def test_reply_prompt_surfaces_scene_objective_before_dialogue_history() -> None:
    ctx = make_ctx(
        target_char_name="星風 瑠奈",
        target_last_utterance_excerpt="まだ帰らないの？",
        reply_focus_text="帰るかどうかの確認",
        scene_objective_text="この場の争点: 張り合いが表に出る",
        recent_dialogue_lines=["星風 瑠奈: 「まだ帰らないの？」"],
    )

    _, user_prompt = build_reply_prompt(ctx)

    assert "この場面の流れ" in user_prompt
    assert "張り合いが表に出る" in user_prompt
    assert user_prompt.index("この場面の流れ") < user_prompt.index("直前の発言:")
    # 新設計: reply_focus_text は user_prompt に出ない
    assert "今返す一点" not in user_prompt


def test_reply_prompt_prioritizes_reply_axes_before_ambient_sections() -> None:
    ctx = make_ctx(
        target_char_name="星風 瑠奈",
        target_last_utterance_excerpt="まだ帰らないの？",
        reply_focus_text="帰るかどうかの確認",
        scene_objective_text="この場の争点: 張り合いが表に出る",
        dominant_signal_text="張り合いに触れると場が動きやすい。",
        dramatic_pressure_texts=["張り合いに触れると場が動きやすい。"],
        ambient_texts=["廊下側が少し騒がしい。"],
        recent_dialogue_lines=["星風 瑠奈: 「まだ帰らないの？」"],
    )

    _, user_prompt = build_reply_prompt(ctx)

    assert user_prompt.index("この場面の流れ") < user_prompt.index("話しかけてきた相手")
    assert user_prompt.index("今の場の動き") < user_prompt.index("【今この場で動きそうなこと】")
    # 新設計: reply_focus_text は user_prompt に出ない
    assert "今返す一点" not in user_prompt
    # dramatic_pressure と ambient は voiced_base_lines に維持されている
    assert "今この場で動きそうなこと" in user_prompt
    assert "周囲の環境" in user_prompt


def test_body_state_section_in_system_prompt():
    """body_state_texts があると system prompt に身体感覚セクションが出る。"""
    ctx = make_ctx(body_state_texts=["少し空腹を感じている。"])
    system_prompt, _ = build_monologue_prompt(ctx)
    assert "身体感覚" in system_prompt
    assert "少し空腹" in system_prompt


def test_ambient_section_in_user_prompt():
    """ambient_texts があると user prompt に周囲の環境セクションが出る。"""
    ctx = make_ctx(ambient_texts=["廊下側が少し騒がしい。"])
    _, user_prompt = build_monologue_prompt(ctx)
    assert "周囲の環境" in user_prompt
    assert "廊下側が少し騒がしい" in user_prompt


def test_group_prompt_contains_recent_dialogue_lines():
    """group prompt は最近の会話断片と参加者一覧を含む。"""
    ctx = make_ctx(
        conversation_partner_names=["星風 瑠奈", "ミリティア"],
        recent_dialogue_lines=[
            "星風 瑠奈: 「音、聞こえた？」",
            "ミリティア: 「今の、誰かいたでしょ」",
        ],
    )
    _, user_prompt = build_group_prompt(ctx)
    assert "星風 瑠奈" in user_prompt
    assert "ミリティア" in user_prompt
    assert "今の、誰かいたでしょ" in user_prompt
    assert "自然に返してください" in user_prompt


def test_group_prompt_can_include_handoff_target_name():
    """group prompt は handoff target があるとその相手への返球を明示する。"""
    ctx = make_ctx(
        conversation_partner_names=["星風 瑠奈", "ミリティア"],
        target_char_name="星風 瑠奈",
        handoff_target_name="星風 瑠奈",
        recent_dialogue_lines=[
            "星風 瑠奈: 「音、聞こえた？」",
            "ミリティア: 「今の、誰かいたでしょ」",
        ],
    )
    _, user_prompt = build_group_prompt(ctx)
    assert "星風 瑠奈" in user_prompt
    # 新設計: 「2文目は {target} に向けて」形式
    assert "2文目は" in user_prompt or "星風 瑠奈 に向けて" in user_prompt


def test_group_prompt_prioritizes_scene_flow_and_handoff() -> None:
    """group prompt は場面の流れと返球先を短く強調する。"""
    ctx = make_ctx(
        conversation_partner_names=["星風 瑠奈", "ミリティア"],
        handoff_target_name="星風 瑠奈",
        scene_objective_text="この場の争点: 張り合いが表に出る",
        recent_dialogue_lines=[
            "ミリティア: 「その顔で勝ったつもり？」",
        ],
    )

    _, user_prompt = build_group_prompt(ctx)

    assert "この場面の流れ" in user_prompt
    assert "この場の争点" not in user_prompt
    # 新設計: voiced_group_prompt の指示文
    assert "自然に返してください" in user_prompt
    assert "2文目" in user_prompt
    assert "星風 瑠奈" in user_prompt


def test_reply_prompt_has_quote_block_and_voice_anchor() -> None:
    """reply prompt は引用ブロックと voice anchor を持つ（focus_text の user_prompt への露出はなし）。"""
    ctx = make_ctx(
        target_char_name="星風 瑠奈",
        target_last_utterance_excerpt="まだ帰らないの？",
        reply_focus_text="帰るかどうかの確認",
        voice_anchor_text="熱血直球で短く返す",
    )

    system_prompt, user_prompt = build_reply_prompt(ctx)

    assert "熱血直球で短く返す" in system_prompt
    assert "まだ帰らないの？" in user_prompt  # 引用ブロックに含まれる
    # 新設計: reply_focus_text は user_prompt に出ない
    assert "今返す一点" not in user_prompt


# --- system_prompt: ストーリー記憶 ---

def test_story_memory_in_system_prompt():
    """story_memory_texts あり → system_prompt に【最近の重要な出来事】セクションを含む"""
    ctx = make_ctx(story_memory_texts=["アリスが謎の石碑を発見した。"])
    system_prompt, _ = build_monologue_prompt(ctx)
    assert "最近の重要な出来事" in system_prompt
    assert "アリスが謎の石碑を発見した。" in system_prompt


def test_story_memory_section_omitted_when_empty():
    """story_memory_texts=[] → system_prompt に【最近の重要な出来事】セクションを含まない"""
    ctx = make_ctx(story_memory_texts=[])
    system_prompt, _ = build_monologue_prompt(ctx)
    assert "最近の重要な出来事" not in system_prompt


def test_intervention_texts_in_system_prompt():
    """intervention_texts あり → system_prompt に【世界の状況変化】セクションを含む"""
    ctx = make_ctx(intervention_texts=["今夜、嵐が来る。"])
    system_prompt, _ = build_monologue_prompt(ctx)
    assert "世界の状況変化" in system_prompt
    assert "今夜、嵐が来る。" in system_prompt


def test_intervention_section_omitted_when_empty():
    """intervention_texts=[] → system_prompt に【世界の状況変化】セクションを含まない"""
    ctx = make_ctx(intervention_texts=[])
    system_prompt, _ = build_monologue_prompt(ctx)
    assert "世界の状況変化" not in system_prompt


def test_active_hooks_in_system_prompt():
    """active_hook_texts あり → system_prompt に未解決フックを含む。"""
    ctx = make_ctx(active_hook_texts=["ルナとの約束: 放課後に屋上で会う。"])
    system_prompt, _ = build_monologue_prompt(ctx)
    assert "未解決のフック" in system_prompt
    assert "ルナとの約束" in system_prompt


def test_relationship_summaries_in_system_prompt():
    """relationship_summary_texts あり → system_prompt に関係サマリを含む。"""
    ctx = make_ctx(relationship_summary_texts=["星風 瑠奈への信頼は 0.62、緊張は 0.18。"])
    system_prompt, _ = build_monologue_prompt(ctx)
    assert "対人関係の現在地" in system_prompt
    assert "信頼は 0.62" in system_prompt


def test_relationship_modes_and_canon_bits_in_system_prompt():
    """relationship/canon signal は system prompt の補助セクションへ入る。"""
    ctx = make_ctx(
        relationship_mode_texts=["星風 瑠奈には認めつつも張り合いがち。"],
        canon_bit_texts=["音楽室では status_clash が起きやすい。"],
    )
    system_prompt, _ = build_monologue_prompt(ctx)
    assert "関係の癖" in system_prompt
    assert "張り合いがち" in system_prompt
    assert "繰り返しがちな流れ" in system_prompt
    assert "status_clash が起きやすい" in system_prompt


def test_dramatic_pressures_in_reply_user_prompt():
    """pressure signal は reply の user prompt に入る。"""
    ctx = make_ctx(
        target_char_name="星風 瑠奈",
        recent_dialogue_lines=["星風 瑠奈: 「……まだ帰らないの？」"],
        dramatic_pressure_texts=["張り合いに触れると場が動きやすい。"],
    )
    _, user_prompt = build_reply_prompt(ctx)
    assert "今この場で動きそうなこと" in user_prompt
    assert "張り合いに触れると場が動きやすい" in user_prompt


def test_dominant_signal_and_scene_objective_are_prioritized_in_reply_prompt():
    """reply prompt は dominant signal と scene objective を短く優先表示する。"""
    ctx = make_ctx(
        target_char_name="星風 瑠奈",
        recent_dialogue_lines=["星風 瑠奈: 「……まだ帰らないの？」"],
        relationship_mode_texts=["星風 瑠奈には認めつつも張り合いがち。"],
        canon_bit_texts=["音楽室では status_clash が起きやすい。"],
        dramatic_pressure_texts=["張り合いに触れると場が動きやすい。"],
        dominant_signal_text="星風 瑠奈には認めつつも張り合いがち。",
        scene_objective_text="この場の争点: 張り合いが表に出る",
    )

    system_prompt, user_prompt = build_reply_prompt(ctx)

    assert "いま強く出ている流れ" in system_prompt
    assert "張り合いがち" in system_prompt
    assert "この場面の流れ" in user_prompt
    assert "張り合いが表に出る" in user_prompt
    assert not any(term in user_prompt for term in LLM_VISIBLE_META_TERMS)


def test_monologue_prompt_omits_generic_scene_objective() -> None:
    """generic objective は prompt に出さない。"""
    ctx = make_ctx(
        dominant_signal_text="自分は misunderstanding の流れを繰り返しやすい。",
        scene_objective_text="自然発生の会話",
    )

    system_prompt, user_prompt = build_monologue_prompt(ctx)

    assert "いま強く出ている流れ" in system_prompt
    assert "この場の争点" not in user_prompt


def test_system_prompt_limits_speech_examples_to_five() -> None:
    """speech example は tone anchor として 5 件までに圧縮する。"""
    ctx = make_ctx(
        speech_examples=["「別に。」", "「……そうか。」", "「好きにしろ。」", "「やれよ。」", "「知らん。」", "「無視。」"],
    )

    system_prompt, _ = build_monologue_prompt(ctx)

    assert "「別に。」" in system_prompt
    assert "「……そうか。」" in system_prompt
    assert "「好きにしろ。」" in system_prompt
    assert "「やれよ。」" in system_prompt
    assert "「知らん。」" in system_prompt
    assert "「無視。」" not in system_prompt  # 6件目は出力されない


def test_voiced_reply_prompt_injects_inner_thought() -> None:
    """build_voiced_reply_prompt は inner_thought を【あなたの今の心の中】として注入する。"""
    ctx = make_ctx(
        target_char_name="星風 瑠奈",
        recent_dialogue_lines=["星風 瑠奈: 「まだ帰らないの？」"],
    )
    _, user_prompt = build_voiced_reply_prompt(ctx, "（まだ帰れない、ここを離れたくない）")

    assert "【あなたの今の心の中】" in user_prompt
    assert "まだ帰れない、ここを離れたくない" in user_prompt
    assert "口に出す言葉を出してください" in user_prompt
    # scaffold は出ない
    assert "【返答の骨格" not in user_prompt
    assert "拾う一点:" not in user_prompt


def test_voiced_reply_prompt_without_inner_thought_still_works() -> None:
    """inner_thought が空でも voiced_reply_prompt は正常に動作する。"""
    ctx = make_ctx(
        target_char_name="星風 瑠奈",
        recent_dialogue_lines=["星風 瑠奈: 「まだ帰らないの？」"],
    )
    _, user_prompt = build_voiced_reply_prompt(ctx, "")

    # inner_thought セクションは出ない
    assert "【あなたの今の心の中】" not in user_prompt
    # 引用ブロックは出る
    assert "星風 瑠奈 がこう言った:" in user_prompt


def test_inner_thought_prompt_presents_target_quote_as_stimulus() -> None:
    """build_inner_thought_prompt は相手の最新発言を刺激として提示する。"""
    ctx = make_ctx(
        target_char_name="星風 瑠奈",
        recent_dialogue_lines=["星風 瑠奈: 「まだ帰らないの？」"],
    )
    _, user_prompt = build_inner_thought_prompt(ctx)

    assert "星風 瑠奈 がこう言った" in user_prompt
    assert "まだ帰らないの？" in user_prompt
    assert "心の中でどう思いますか" in user_prompt
    assert "（　）でくくって" in user_prompt


def test_build_voiced_group_prompt_injects_inner_thought() -> None:
    """build_voiced_group_prompt は inner_thought を注入する。"""
    ctx = make_ctx(
        conversation_partner_names=["星風 瑠奈", "ミリティア"],
        recent_dialogue_lines=["ミリティア: 「その顔で勝ったつもり？」"],
    )
    _, user_prompt = build_voiced_group_prompt(ctx, "（負けた気がしない）")

    assert "【あなたの今の心の中】" in user_prompt
    assert "負けた気がしない" in user_prompt


def test_reply_prompt_includes_place_dialogue_lines() -> None:
    """place_dialogue_lines が reply prompt の '滞在中の流れ' セクションに出力されること。"""
    ctx = make_ctx(
        target_char_name="相手",
        place_dialogue_lines=["相手: こんにちは", "夜風 勇真: やあ"],
        recent_dialogue_lines=["相手: どうぞ"],
    )
    _, user_prompt = build_reply_prompt(ctx)

    assert "滞在中の流れ" in user_prompt
    assert "こんにちは" in user_prompt


def test_reply_prompt_omits_place_dialogue_section_when_empty() -> None:
    """place_dialogue_lines が空の場合、'滞在中の流れ' セクションは出力されないこと。"""
    ctx = make_ctx(target_char_name="相手")
    _, user_prompt = build_reply_prompt(ctx)

    assert "滞在中の流れ" not in user_prompt


def test_monologue_prompt_includes_place_dialogue_lines() -> None:
    """place_dialogue_lines が monologue prompt に出力されること。"""
    ctx = make_ctx(
        place_dialogue_lines=["相手: 何をしているの？", "夜風 勇真: 練習中"],
    )
    _, user_prompt = build_monologue_prompt(ctx)

    assert "滞在中の流れ" in user_prompt
    assert "練習中" in user_prompt


def test_group_prompt_includes_place_dialogue_lines() -> None:
    """place_dialogue_lines が group prompt に出力されること。"""
    ctx = make_ctx(
        conversation_partner_names=["相手"],
        place_dialogue_lines=["相手: よろしく", "夜風 勇真: ああ"],
    )
    _, user_prompt = build_group_prompt(ctx)

    assert "滞在中の流れ" in user_prompt
    assert "よろしく" in user_prompt


def test_initiation_prompt_includes_place_dialogue_lines() -> None:
    """place_dialogue_lines が initiation prompt に出力されること。"""
    ctx = make_ctx(
        target_char_name="相手",
        is_initiation=True,
        place_dialogue_lines=["相手: 待ってたよ"],
    )
    _, user_prompt = build_initiation_prompt(ctx)

    assert "滞在中の流れ" in user_prompt
    assert "待ってたよ" in user_prompt

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import logging

from engine.story_style import StoryStyleProfile

logger = logging.getLogger(__name__)

_BACKEND_META_TERMS = (
    "判断基準",
    "論点",
    "争点",
    "押しどころ",
    "主導権",
    "食い違い",
)


def _find_target_last_utterance(
    recent_dialogue_lines: list[str],
    target_char_name: str | None,
) -> str | None:
    """recent_dialogue_lines から target_char_name の最新発言テキストを返す。

    "名前: 発言" 形式を前提とし、発言部分のみを抽出する。
    target_char_name が None の場合は最後の行を返す。
    """
    if not recent_dialogue_lines:
        return None
    for line in reversed(recent_dialogue_lines):
        if target_char_name is None or target_char_name in line:
            colon_pos = line.find(": ")
            if colon_pos != -1:
                return line[colon_pos + 2:].strip()
            return line.strip()
    # fallback: 最新行の発言部分
    last = recent_dialogue_lines[-1]
    colon_pos = last.find(": ")
    return last[colon_pos + 2:].strip() if colon_pos != -1 else last.strip()


def _build_place_dialogue_section(ctx: "CharacterContext") -> list[str]:
    """滞在中の会話履歴セクションを行リストで返す。空なら空リスト。"""
    if not ctx.place_dialogue_lines:
        return []
    return [f"滞在中の流れ ({ctx.current_place_name}):", *ctx.place_dialogue_lines]


def _clean_dialogue_excerpt(text: str | None) -> str | None:
    if not text:
        return None
    cleaned = str(text).strip()
    quote_pairs = (("「", "」"), ("『", "』"), ('"', '"'), ("'", "'"))
    changed = True
    while changed and len(cleaned) >= 2:
        changed = False
        for left, right in quote_pairs:
            if cleaned.startswith(left) and cleaned.endswith(right):
                cleaned = cleaned[len(left):-len(right)].strip()
                changed = True
                break
    return cleaned or None


def _contains_backend_meta_vocabulary(text: str | None) -> bool:
    if not text:
        return False
    return any(term in text for term in _BACKEND_META_TERMS)


def _format_reply_focus_prompt_text(reply_focus_text: str | None) -> str | None:
    if not reply_focus_text:
        return None
    # メタ語彙が含まれる場合は行ごと出力しない。
    # 汎用テキスト ("相手が気にしている一点") に置換すると LLM への文脈がゼロになり
    # 返答が硬直する。何も言わない方が自然な台詞を引き出せる。
    if _contains_backend_meta_vocabulary(reply_focus_text):
        return None
    return reply_focus_text


def _format_scene_objective_prompt_text(scene_objective_text: str | None) -> str | None:
    if not scene_objective_text or scene_objective_text == "自然発生の会話":
        return None
    objective_text = scene_objective_text.replace("この場の争点:", "").strip()
    if not objective_text:
        return None
    if _contains_backend_meta_vocabulary(objective_text):
        return None  # 汎用テキストより非表示の方が LLM に良い
    return objective_text


def _reply_focus_first_sentence_guidance(reply_focus_text: str | None) -> str | None:
    if not reply_focus_text:
        return None
    return "1文目は自分の気持ちや立場に正直な反応を、キャラクターとして自然な言葉で出してください。"


def _reply_focus_second_sentence_guidance(reply_focus_text: str | None) -> str | None:
    if not reply_focus_text:
        return None
    return "2文目を使う場合は、相手への問いかけ・感情・具体的な行動の提案のどれか1つで返してください。"


@dataclass
class CharacterContext:
    """prompt_builder に渡すキャラクターコンテキスト（Phase 2 版）。"""
    # キャラ基本情報（characters テーブルから取得）
    char_id: str
    name_ja: str
    personality_core: str
    first_person: str           # speech["first_person"]
    tone: str                   # speech["tone"]
    speech_examples: list[str]  # speech["examples"]
    never_say: list[str]        # speech["never_say"]（空リスト可）
    current_goal: str
    current_worry: str
    # 現在状態（character_states テーブルから取得）
    sim_datetime: str           # "YYYY-MM-DDTHH:MM"
    current_place_name: str     # place_id を日本語名に変換済み
    # Phase 2 追加フィールド（デフォルト値あり → Phase 1 テストとの後方互換性を保つ）
    emotions: dict[str, float] = field(default_factory=dict)
    current_expression: str = "neutral"
    move_reason: str | None = None
    anomaly: dict[str, Any] | None = None
    same_place_char_names: list[str] = field(default_factory=list)
    secret_hint: str | None = None
    memory_texts: list[str] = field(default_factory=list)
    story_memory_texts: list[str] = field(default_factory=list)     # Phase A-6: ストーリー記憶
    intervention_texts: list[str] = field(default_factory=list)    # Phase B-9: 世界状況変化
    active_hook_texts: list[str] = field(default_factory=list)
    relationship_summary_texts: list[str] = field(default_factory=list)
    relationship_mode_texts: list[str] = field(default_factory=list)
    canon_bit_texts: list[str] = field(default_factory=list)
    dramatic_pressure_texts: list[str] = field(default_factory=list)
    dominant_signal_text: str | None = None
    scene_objective_text: str | None = None
    story_pressure_cue: str | None = None
    target_last_utterance_excerpt: str | None = None
    reply_focus_text: str | None = None
    voice_anchor_text: str | None = None
    body_state_texts: list[str] = field(default_factory=list)
    ambient_texts: list[str] = field(default_factory=list)
    chapter_world_injection: str | None = None  # v2 upgrade: Chapter System
    director_tone_hints: list[str] = field(default_factory=list)  # v2 upgrade: Director Persona
    pre_turn_scene_script: str | None = None  # pre-turn scene script from director
    character_directive_text: str | None = None  # structured scene script の自分専用実行カード
    scene_frame_text: str | None = None
    scene_turn_goal_text: str | None = None
    scene_turn_shift_text: str | None = None
    scene_banned_surface_patterns: list[str] = field(default_factory=list)
    speaker_initiative: bool = False           # advance intent: 宣言促進
    is_presence_monologue: bool = False        # 他キャラ同席中の内省モノローグ
    speaker_objective: str | None = None       # シーンスクリプトから抽出した今ターンの動き（内的）
    reaction_frame: str | None = None          # 感情+信頼度から導出した内的スタンス
    target_char_personality_hint: str | None = None  # 相手キャラの性格ヒント
    relationship_frame: str | None = None      # 相手との関係ラベル
    is_initiation: bool = False                # 自分から会話を始めるターン
    target_char_name: str | None = None
    handoff_target_name: str | None = None
    conversation_partner_names: list[str] = field(default_factory=list)
    recent_dialogue_lines: list[str] = field(default_factory=list)
    place_dialogue_lines: list[str] = field(default_factory=list)
    style_profile: StoryStyleProfile | None = None
    max_utterance_chars: int = 180  # per-story の発話文字数上限（quality_guard と連動）
    max_sentences: int = 2          # per-story の発話文数上限（quality_guard と連動）


def _emotion_to_text(emotions: dict[str, float], expression: str) -> str:
    """感情値と表情ラベルを日本語テキストに変換する。"""
    _EXPRESSION_JA: dict[str, str] = {
        "happy":    "嬉しい気分",
        "angry":    "怒りを感じている",
        "sad":      "悲しい気分",
        "surprised": "驚いている",
        "worried":  "不安を感じている",
        "content":  "満足している",
        "lonely":   "寂しい気分",
        "tired":    "疲れた気分",
        "neutral":  "普通の気分",
    }
    base = _EXPRESSION_JA.get(expression, "普通の気分")

    modifiers: list[str] = []
    if emotions.get("stress", 0.5) > 0.6:
        modifiers.append("ストレスが高い")
    motivation = emotions.get("motivation", 0.5)
    if motivation > 0.7:
        modifiers.append("やる気がある")
    elif motivation < 0.3:
        modifiers.append("やる気が低い")
    if emotions.get("loneliness", 0.5) > 0.6:
        modifiers.append("孤独感がある")
    if emotions.get("excitement", 0.5) > 0.7:
        modifiers.append("興奮気味")

    return f"{base}（{', '.join(modifiers)}）" if modifiers else base


def _build_system_prompt(ctx: CharacterContext) -> str:
    """発言種別に依らない system_prompt 共通部を返す。"""
    examples_text = "\n".join(ctx.speech_examples[:5])

    never_say_section = ""
    if ctx.never_say:
        never_say_section = f"\n【絶対に使わない言葉】\n{', '.join(ctx.never_say)}\n"

    # Phase 2: 感情セクション（emotions が空でない場合のみ）
    emotion_section = ""
    if ctx.emotions:
        emotion_text = _emotion_to_text(ctx.emotions, ctx.current_expression)
        emotion_section = f"\n【現在の感情】\n{emotion_text}\n"

    # Phase 2: 記憶セクション（memory_texts が空でない場合のみ）
    memory_section = ""
    if ctx.memory_texts:
        memory_lines = "\n".join(f"- {m}" for m in ctx.memory_texts)
        memory_section = f"\n【最近の記憶】\n{memory_lines}\n"

    # Phase A-6: ストーリー記憶セクション（story_memory_texts が空でない場合のみ）
    story_memory_section = ""
    if ctx.story_memory_texts:
        story_memory_lines = "\n".join(f"- {m}" for m in ctx.story_memory_texts)
        story_memory_section = f"\n【最近の重要な出来事】\n{story_memory_lines}\n"

    # Phase B-9: 介入セクション（intervention_texts が空でない場合のみ）
    intervention_section = ""
    if ctx.intervention_texts:
        intervention_lines = "\n".join(f"- {t}" for t in ctx.intervention_texts)
        intervention_section = f"\n【世界の状況変化】\n{intervention_lines}\n"

    hook_section = ""
    if ctx.active_hook_texts:
        hook_lines = "\n".join(f"- {t}" for t in ctx.active_hook_texts)
        hook_section = f"\n【未解決のフック】\n{hook_lines}\n"

    relationship_section = ""
    if ctx.relationship_summary_texts:
        relationship_lines = "\n".join(f"- {t}" for t in ctx.relationship_summary_texts)
        relationship_section = f"\n【対人関係の現在地】\n{relationship_lines}\n"

    relationship_mode_section = ""
    if ctx.relationship_mode_texts:
        relationship_mode_lines = "\n".join(f"- {t}" for t in ctx.relationship_mode_texts)
        relationship_mode_section = f"\n【関係の癖】\n{relationship_mode_lines}\n"

    canon_section = ""
    if ctx.canon_bit_texts:
        canon_lines = "\n".join(f"- {t}" for t in ctx.canon_bit_texts)
        canon_section = f"\n【繰り返しがちな流れ】\n{canon_lines}\n"

    dominant_signal_section = ""
    if ctx.dominant_signal_text:
        dominant_signal_section = f"\n【いま強く出ている流れ】\n- {ctx.dominant_signal_text}\n"

    story_pressure_section = ""
    if ctx.story_pressure_cue:
        story_pressure_section = f"\n【今気になっていること】\n- {ctx.story_pressure_cue}\n"

    voice_anchor_section = ""
    if ctx.voice_anchor_text:
        voice_anchor_section = f"\n【返し方の芯】\n- {ctx.voice_anchor_text}\n"

    body_state_section = ""
    if ctx.body_state_texts:
        body_state_lines = "\n".join(f"- {t}" for t in ctx.body_state_texts)
        body_state_section = f"\n【身体感覚】\n{body_state_lines}\n"

    # Phase 2: 秘密ヒントセクション（secret_hint がある場合のみ）
    secret_section = ""
    if ctx.secret_hint is not None:
        secret_section = f"\n【心の内（内部情報：LLMのみ参照）】\n{ctx.secret_hint}\n"

    # structured scene script の自分専用カードを優先し、legacy text は fallback でのみ使う。
    scene_script_section = ""
    scene_brief_lines: list[str] = []
    if ctx.scene_frame_text:
        scene_brief_lines.append(f"シーン: {ctx.scene_frame_text}")
    if ctx.scene_turn_goal_text:
        scene_brief_lines.append(f"ターン目的: {ctx.scene_turn_goal_text}")
    if ctx.scene_turn_shift_text:
        scene_brief_lines.append(f"転機: {ctx.scene_turn_shift_text}")
    if scene_brief_lines:
        scene_script_section = (
            "\n【このターンの場面設計】\n"
            + "\n".join(scene_brief_lines)
            + "\n（これは内部指示です。台詞にそのまま出さない）\n"
        )
    if ctx.character_directive_text:
        scene_script_section += (
            "\n【監督からあなたへの実行カード】\n"
            f"{ctx.character_directive_text}\n"
            "（これは内部指示です。台詞にそのまま出さない）\n"
        )
    elif ctx.pre_turn_scene_script and not scene_brief_lines:
        scene_script_section += (
            "\n【演出家からの演技指導（内的指示。台詞には出さない）】\n"
            f"{ctx.pre_turn_scene_script}\n"
            "（演技指導ここまで）\n"
        )

    # v2 upgrade: Chapter System — world_injection（active chapter がある場合のみ）
    chapter_section = ""
    if ctx.chapter_world_injection:
        chapter_section = f"\n【現在の情景・状況】\n{ctx.chapter_world_injection}\n"

    # v2 upgrade: Director Persona — tone hints（active persona がある場合のみ）
    director_section = ""
    if ctx.director_tone_hints:
        hints_text = "\n".join(f"・{h}" for h in ctx.director_tone_hints)
        director_section = f"\n【演出家からの指示】\n{hints_text}\n"

    # セクション並び順: [A identity] → [B background] → [C current state] → [D turn directives] → [E rules]
    # SLM（小規模言語モデル）の「Lost in the Middle」対策:
    # SLM は先頭（attention sink）と末尾に最も注意を払う。
    # turn directive（演技指導・監督カード・voice anchor）は末尾寄りに置くことで
    # SLM が確実に遵守する確率が高まる。
    if ctx.max_utterance_chars <= 180:
        length_rule = f"- 発話の文字数は最大 {ctx.max_utterance_chars} 字以内にしてください（URL を除く）\n"
    else:
        length_rule = (
            "- 通常は180字以内で、短く自然に言い切ってください\n"
            f"- 必要な場合だけ最大 {ctx.max_utterance_chars} 字まで使えます（URL を除く）\n"
        )

    return (
        # === A: Identity（先頭・attention sink）— キャラの核 ===
        f"あなたは「{ctx.name_ja}」というキャラクターです。\n"
        "\n"
        "【性格・口調】\n"
        f"{ctx.personality_core}\n"
        "\n"
        "【一人称】\n"
        f"「{ctx.first_person}」を使ってください。\n"
        "\n"
        "【口調の特徴】\n"
        f"{ctx.tone}\n"
        "\n"
        "【口調例】\n"
        f"{examples_text}\n"
        "\n"
        "【現在の目標】\n"
        f"{ctx.current_goal}\n"
        "\n"
        "【現在の悩み】\n"
        f"{ctx.current_worry}\n"
        f"{never_say_section}"
        # === B: Background knowledge（中間・多少失っても致命的でない）===
        f"{memory_section}"
        f"{story_memory_section}"
        f"{intervention_section}"
        f"{hook_section}"
        f"{relationship_section}"
        f"{relationship_mode_section}"
        f"{canon_section}"
        # === C: Current state（中盤後半・今このターンの状態）===
        f"{emotion_section}"
        f"{body_state_section}"
        f"{dominant_signal_section}"
        f"{story_pressure_section}"
        f"{secret_section}"
        f"{chapter_section}"
        # === D: Turn directives（末尾直前・SLM が最後に読む高優先指示）===
        f"{director_section}"
        f"{voice_anchor_section}"
        f"{scene_script_section}"
        # === E: Rules（最末尾・必ず遵守させる）===
        "\n"
        "【ルール】\n"
        f"- 発言は最大 {ctx.max_sentences} 文以内にしてください。{ctx.max_sentences + 1} 文以上は避けてください\n"
        + length_rule
        + "- キャラクターとして自然な言葉で話してください\n"
        "- 余計な説明や地の文は不要です。発言本文だけを出力してください\n"
        "- Markdown 記法は使わないでください\n"
        "- ** や * や # や箇条書きなどの装飾記号を付けないでください\n"
        "- 強調したい時も記号ではなく言葉で表現してください\n"
        "- 具体的な相手・物・行動を優先し、抽象的な独白だけで終わらせないでください"
        + (
            "\n"
            + "\n".join(f"- {rule}" for rule in ctx.style_profile.dialogue_rules)
            if ctx.style_profile is not None and ctx.style_profile.dialogue_rules
            else ""
        )
    )


def _build_situation_lines(ctx: CharacterContext) -> list[str]:
    """発言状況を表す user_prompt の共通行を返す。"""
    lines = [
        "【現在の状況】",
        f"日時: {ctx.sim_datetime}",
        f"場所: {ctx.current_place_name}",
    ]

    same_place_line = ""
    if ctx.same_place_char_names:
        same_place_line = f"周囲にいる人: {', '.join(ctx.same_place_char_names)}\n"
        lines.append(same_place_line.rstrip())

    move_line = ""
    if ctx.move_reason is not None:
        move_line = f"直前の行動: {ctx.move_reason}\n"
        lines.append(move_line.rstrip())

    anomaly_line = ""
    if ctx.anomaly is not None:
        anomaly_line = f"気になること: {ctx.anomaly['label']}\n"
        lines.append(anomaly_line.rstrip())

    if ctx.ambient_texts:
        lines.append("周囲の環境:")
        lines.extend(f"- {text}" for text in ctx.ambient_texts)

    if ctx.dramatic_pressure_texts:
        lines.append("【今この場で動きそうなこと】")
        lines.extend(f"- {text}" for text in ctx.dramatic_pressure_texts)

    objective_text = _format_scene_objective_prompt_text(ctx.scene_objective_text)
    if objective_text:
        lines.append(f"この場面の流れ: {objective_text}")

    return lines


def build_monologue_prompt(ctx: CharacterContext) -> tuple[str, str]:
    """独り言ターン用の (system_prompt, user_prompt) を返す。"""
    system_prompt = _build_system_prompt(ctx)

    lines = _build_situation_lines(ctx)
    place_section = _build_place_dialogue_section(ctx)
    if place_section:
        lines.append("")
        lines.extend(place_section)
    if ctx.dominant_signal_text:
        lines.append(f"今の場の流れ: {ctx.dominant_signal_text}")

    if ctx.is_presence_monologue:
        # 他キャラ同席中の内省モノローグ（presence monologue）
        monologue_instructions = [
            "今、あなたは周囲に人がいながら、内心で何かを考えています。",
            "台詞ではなく、内心の声を（　）でくくって1〜2文で出力してください。",
            "具体的な人・物・出来事の一点に触れて、そこから何かを感じ取るか、何かを決意してください。",
            "抽象的な感傷だけで終わらせないでください。",
        ]
    else:
        # 物理的に一人でいる状態の独り言
        monologue_instructions = [
            "今いる場所で、独り言を言ってください。",
            "今の場の流れや気になることがあれば、その一点だけを自然に拾ってください。",
            "1〜2文で、具体的な物・相手・行動のどれかを必ず含めてください。",
            "抽象的な感傷や比喩だけで終わらせないでください。",
        ]

    user_prompt = "\n".join(
        lines + ["", *monologue_instructions]
    )

    logger.debug(
        "Built monologue prompt",
        extra={"char_id": ctx.char_id, "sim_datetime": ctx.sim_datetime},
    )

    return system_prompt, user_prompt


def build_reply_prompt(ctx: CharacterContext) -> tuple[str, str]:
    """1対1会話の返答用 prompt を返す（inner_thought なしの単体版）。

    story_engine の 2 段生成では build_voiced_reply_prompt が呼ばれる。
    この関数は quality_guard / test の参照を維持するためのブリッジ。
    """
    return build_voiced_reply_prompt(ctx, "")


def build_group_prompt(ctx: CharacterContext) -> tuple[str, str]:
    """グループ会話用 prompt を返す（inner_thought なしの単体版）。

    story_engine の 2 段生成では build_voiced_group_prompt が呼ばれる。
    この関数は quality_guard / test の参照を維持するためのブリッジ。
    """
    return build_voiced_group_prompt(ctx, "")


def build_initiation_prompt(ctx: CharacterContext) -> tuple[str, str]:
    """自発声かけターン用 prompt を返す（inner_thought なしの単体版）。

    story_engine の 2 段生成では build_voiced_initiation_prompt が呼ばれる。
    この関数は quality_guard / test の参照を維持するためのブリッジ。
    """
    return build_voiced_initiation_prompt(ctx, "")


# ============================================================
# 2段生成: 内的モノローグ → 発話 (「俳優として演じる」設計)
# ============================================================

def build_inner_thought_prompt(ctx: CharacterContext) -> tuple[str, str]:
    """reply/group/initiation ターン用の「内的モノローグ生成」 prompt を返す。

    LLM に先に「心の中で何を思うか」を（）で1文生成させ、
    その内心を材料に発話生成 (build_voiced_*) へ渡す。
    """
    system_prompt = _build_system_prompt(ctx)

    lines = [
        "【現在の状況】",
        f"日時: {ctx.sim_datetime}",
        f"場所: {ctx.current_place_name}",
    ]

    # 相手キャラ情報を散文で（scaffold ラベルは使わない）
    inner_context_parts: list[str] = []
    if ctx.target_char_name:
        inner_context_parts.append(f"相手: {ctx.target_char_name}")
        if ctx.target_char_personality_hint:
            inner_context_parts.append(
                f"{ctx.target_char_name}という人: {ctx.target_char_personality_hint}"
            )
        if ctx.relationship_frame:
            inner_context_parts.append(f"あなたとの関係: {ctx.relationship_frame}")
    if ctx.reaction_frame:
        inner_context_parts.append(f"今のあなたの内的状態: {ctx.reaction_frame}")
    if ctx.speaker_objective:
        inner_context_parts.append(f"今あなたが動こうとしていること: {ctx.speaker_objective}")
    if inner_context_parts:
        lines.append("")
        lines.extend(inner_context_parts)

    # 相手の最新発言を刺激として提示
    target_utterance = _clean_dialogue_excerpt(
        _find_target_last_utterance(ctx.recent_dialogue_lines, ctx.target_char_name)
        or ctx.target_last_utterance_excerpt
    )
    if target_utterance and ctx.target_char_name:
        lines += [
            "",
            f"━━━ {ctx.target_char_name} がこう言った ━━━",
            f"「{target_utterance}」",
            "━━━━━━━━━━━━━━━━━━━━━",
        ]
    elif ctx.recent_dialogue_lines:
        lines.append("")
        lines.append("直近の流れ:")
        lines.extend(ctx.recent_dialogue_lines[-3:])

    lines += [
        "",
        "あなたは今、この状況を受けて、心の中でどう思いますか？",
        "（　）でくくって、1文だけ、正直に出してください。",
        "自分の言葉で。感情でも、判断でも、疑問でも構いません。",
    ]

    user_prompt = "\n".join(lines)

    logger.debug(
        "Built inner thought prompt",
        extra={"char_id": ctx.char_id, "sim_datetime": ctx.sim_datetime},
    )

    return system_prompt, user_prompt


def _build_voiced_base_lines(ctx: CharacterContext, inner_thought: str) -> list[str]:
    """voiced 系 prompt 共通の状況ブロックを構築する。"""
    lines = [
        "【現在の状況】",
        f"日時: {ctx.sim_datetime}",
        f"場所: {ctx.current_place_name}",
    ]
    objective_text = _format_scene_objective_prompt_text(ctx.scene_objective_text)
    if objective_text:
        lines.append(f"この場面の流れ: {objective_text}")
    if ctx.dominant_signal_text:
        lines.append(f"今の場の動き: {ctx.dominant_signal_text}")
    if ctx.story_pressure_cue:
        lines.append(f"今気になっていること: {ctx.story_pressure_cue}")
    if ctx.target_char_name is not None:
        lines.append(f"話しかけてきた相手: {ctx.target_char_name}")
    if ctx.same_place_char_names:
        lines.append(f"周囲にいる人: {', '.join(ctx.same_place_char_names)}")
    if ctx.move_reason is not None:
        lines.append(f"直前の行動: {ctx.move_reason}")
    if ctx.anomaly is not None:
        lines.append(f"気になること: {ctx.anomaly['label']}")
    if ctx.ambient_texts:
        lines.append("周囲の環境:")
        lines.extend(f"- {text}" for text in ctx.ambient_texts)
    if ctx.dramatic_pressure_texts:
        lines.append("【今この場で動きそうなこと】")
        lines.extend(f"- {text}" for text in ctx.dramatic_pressure_texts)

    place_section = _build_place_dialogue_section(ctx)
    if place_section:
        lines.append("")
        lines.extend(place_section)

    if ctx.recent_dialogue_lines:
        lines.append("直前の発言:")
        lines.extend(ctx.recent_dialogue_lines)

    return lines


def build_voiced_reply_prompt(
    ctx: CharacterContext, inner_thought: str
) -> tuple[str, str]:
    """内的モノローグを受け取り、reply 発話を生成する prompt を返す。

    scaffold（拾う一点/立場/理由/次の一手）は使わず、
    inner_thought を唯一の内的アンカーとして渡す。
    """
    system_prompt = _build_system_prompt(ctx)
    lines = _build_voiced_base_lines(ctx, inner_thought)

    # 相手の最新発言引用ブロック
    target_utterance = _clean_dialogue_excerpt(
        _find_target_last_utterance(ctx.recent_dialogue_lines, ctx.target_char_name)
        or ctx.target_last_utterance_excerpt
    )
    if target_utterance and ctx.target_char_name:
        lines += [
            "",
            "━━━━━━━━━━━━━━━━━━━━━",
            f"{ctx.target_char_name} がこう言った:",
            f"「{target_utterance}」",
            "━━━━━━━━━━━━━━━━━━━━━",
        ]

    # 内心インジェクション
    if inner_thought:
        lines += [
            "",
            f"【あなたの今の心の中】{inner_thought}",
        ]

    initiative_hint = (
        ["（あなたがこの場を先に動かすターンです。疑問文ではなく、宣言・断言・行動意思で始めてください）"]
        if ctx.speaker_initiative
        else []
    )

    instructions = [
        "",
        "↑ この心を踏まえて、口に出す言葉を出してください。",
        f"{ctx.name_ja} として、自然な声で 1〜2 文。",
        *initiative_hint,
    ]

    user_prompt = "\n".join(lines + instructions)

    logger.debug(
        "Built voiced reply prompt",
        extra={"char_id": ctx.char_id, "sim_datetime": ctx.sim_datetime},
    )

    return system_prompt, user_prompt


def build_voiced_group_prompt(
    ctx: CharacterContext, inner_thought: str
) -> tuple[str, str]:
    """内的モノローグを受け取り、group 発話を生成する prompt を返す。"""
    system_prompt = _build_system_prompt(ctx)
    lines = _build_voiced_base_lines(ctx, inner_thought)

    if ctx.conversation_partner_names:
        lines.append(f"会話相手: {', '.join(ctx.conversation_partner_names)}")

    # グループの直近発言引用ブロック
    if ctx.recent_dialogue_lines:
        last_line = ctx.recent_dialogue_lines[-1]
        colon_pos = last_line.find(": ")
        last_speaker = last_line[:colon_pos].strip() if colon_pos != -1 else ""
        last_utterance = last_line[colon_pos + 2:].strip() if colon_pos != -1 else last_line
        if last_utterance:
            lines += [
                "",
                "━━━ 直近の発言 ━━━",
                f"{last_speaker}: 「{last_utterance}」",
                "━━━━━━━━━━━━━",
            ]

    if inner_thought:
        lines += [
            "",
            f"【あなたの今の心の中】{inner_thought}",
        ]

    handoff_hint: list[str] = []
    if ctx.handoff_target_name is not None:
        handoff_hint = [
            f"2文目は {ctx.handoff_target_name} に向けて、挑発する・提案する・宣言するのどれか1つで。",
        ]

    instructions = [
        "",
        "↑ この心を踏まえて、あなたの立場から 1〜2 文で自然に返してください。",
        f"{ctx.name_ja} として、自然な声で。",
        *handoff_hint,
    ]

    user_prompt = "\n".join(lines + instructions)

    logger.debug(
        "Built voiced group prompt",
        extra={"char_id": ctx.char_id, "sim_datetime": ctx.sim_datetime},
    )

    return system_prompt, user_prompt


def build_voiced_initiation_prompt(
    ctx: CharacterContext, inner_thought: str
) -> tuple[str, str]:
    """内的モノローグを受け取り、自発声かけ発話を生成する prompt を返す。"""
    system_prompt = _build_system_prompt(ctx)
    lines = _build_voiced_base_lines(ctx, inner_thought)

    target = ctx.target_char_name or (
        ctx.conversation_partner_names[0] if ctx.conversation_partner_names else None
    )

    lines.append("")
    lines.append("【今あなたが話しかけに行く】")
    if target:
        lines.append(f"声をかける相手: {target}")
        if ctx.target_char_personality_hint and target:
            lines.append(f"{target}という人: {ctx.target_char_personality_hint}")
        if ctx.relationship_frame and target:
            lines.append(f"あなたと{target}の関係: {ctx.relationship_frame}")

    if inner_thought:
        lines += [
            "",
            f"【あなたの今の心の中】{inner_thought}",
        ]

    instructions = [
        "",
        "あなたが先に声をかけます。",
        "この心の声を踏まえて、あなたの最初の言葉を出してください。",
        "具体的な話題・記憶・観察から始めてください。",
        f"{ctx.name_ja} として、自然な声で 1〜2 文。",
    ]

    user_prompt = "\n".join(lines + instructions)

    logger.debug(
        "Built voiced initiation prompt",
        extra={"char_id": ctx.char_id, "sim_datetime": ctx.sim_datetime},
    )

    return system_prompt, user_prompt


# ============================================================
# 時事感想ターン用プロンプト（時事モード）
# ============================================================

def build_current_affairs_prompt(
    ctx: CharacterContext,
    article: dict,
) -> tuple[str, str]:
    """時事感想ターン用の (system_prompt, user_prompt) を返す（1-pass）。

    article: {"title": str, "url": str, ...}
    """
    system_prompt = _build_system_prompt(ctx)

    lines = _build_situation_lines(ctx)
    place_section = _build_place_dialogue_section(ctx)
    if place_section:
        lines.append("")
        lines.extend(place_section)

    article_title = str(article.get("title", "")).strip()
    article_url = str(article.get("url", ""))
    article_description = str(article.get("description") or "").strip()

    lines += [
        "",
        "【こんなニュースタイトルが目に入った】",
        f"「{article_title}」",
    ]
    if article_description:
        # プロンプトへの埋め込みは 100 字に絞る。
        # 300 字全量は LLM が概要文を長文応答に含める原因になり、
        # quality_guard (sentence_count > 2 / overlength 180 字) に弾かれてフォールバックを招く。
        desc_for_prompt = article_description[:100]
        lines.append(f"（記事の概要（参考のみ）: {desc_for_prompt}）")
    lines.append("")

    if ctx.recent_dialogue_lines:
        for dl in ctx.recent_dialogue_lines[-5:]:
            lines.append(dl)
        lines.append("")

    handoff = ctx.handoff_target_name or (
        ctx.same_place_char_names[0] if ctx.same_place_char_names else None
    )

    if handoff:
        lines += [
            f"{handoff} がそばにいる。",
            "",
        ]

    instructions = [
        "---",
        f"{ctx.name_ja} として、このタイトルを見た瞬間の素直なリアクションを 1〜2 文で言ってください。",
        "",
        f"• このタイトルの「具体的な話題・出来事」に {ctx.name_ja} らしく反応すること（驚き・ツッコミ・共感・皮肉など）",
        "• タイトルを全文繰り返す必要はない（システムが「タイトル」として別途表示する）",
        "• 抽象的・哲学的な感想は不可。このタイトルの内容に直接触れること",
        "• 概要（参考）が提供されていても、概要文をそのまま引用・繰り返さないこと",
    ]
    if handoff:
        instructions.append(
            f"• {handoff} がいれば「ねえ {handoff}、これどう思う？」のように話題を振ってもよい"
        )
    instructions += [
        "",
        "文頭に「 を書かないこと。「って」「えっ」など、タイトルを受けた自然な継ぎ言葉か感嘆から始める。URL は書かない。",
    ]

    user_prompt = "\n".join(lines + instructions)

    logger.debug(
        "Built current_affairs prompt",
        extra={
            "char_id": ctx.char_id,
            "article_url": article_url,
            "sim_datetime": ctx.sim_datetime,
        },
    )

    return system_prompt, user_prompt

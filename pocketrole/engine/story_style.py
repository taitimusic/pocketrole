from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class StoryStyleProfile:
    """story ごとの軽量な文体プロファイル。"""

    story_id: str
    dialogue_mode: str = "default"
    narration_mode: str = "default"
    narration_min_interval_turns: int | None = None
    narration_emotion_threshold: float | None = None
    allow_literary_narration: bool = True
    quality_guard_mode: str = "default"
    dialogue_rules: tuple[str, ...] = field(default_factory=tuple)
    narration_rules: tuple[str, ...] = field(default_factory=tuple)


DEFAULT_STYLE_PROFILE = StoryStyleProfile(story_id="default")

# story_mode ベースのプロファイルマップ
# world_config.yaml の story.story_mode → このマップでプロファイルを選択する。
# story_id のハードコード分岐を廃止し、ジャンル宣言で動的に切り替える。
_STYLE_PROFILES_BY_MODE: dict[str, StoryStyleProfile] = {
    "drama": StoryStyleProfile(
        story_id="drama",
        dialogue_mode="mystery_drama",
        narration_mode="atmospheric",
        narration_min_interval_turns=3,
        narration_emotion_threshold=0.40,
        allow_literary_narration=True,
        quality_guard_mode="default",
        dialogue_rules=(
            "具体的な相手・物・行動を優先してください。",
            "発言は1〜2文を基本にしてください。",
            "自分の気持ちや立場を、短く正直に出してください。",
            "詩的な独白や抽象的な感傷は避けてください。",
        ),
        narration_rules=(
            "場所の空気・人物の動き・小さな緊張を短く伝えてください。",
            "1〜2文に収めてください。",
            "比喩は控えめに。",
            "具体的な人物・動作・場の空気を1つだけ拾ってください。",
        ),
    ),
    "comedy": StoryStyleProfile(
        story_id="comedy",
        dialogue_mode="light_banter",
        narration_mode="minimal_comic",
        narration_min_interval_turns=4,
        narration_emotion_threshold=0.40,
        allow_literary_narration=False,
        quality_guard_mode="light_banter",
        dialogue_rules=(
            "具体的な相手・物・行動を優先してください。",
            "発言は1〜2文を基本にしてください。",
            "軽いボケ・ツッコミ・反論・提案のどれか1つに絞ってください。",
            "比喩や詩的な独白は避けてください。",
        ),
        narration_rules=(
            "短い状況メモとして書いてください。",
            "1〜2文に収めてください。",
            "比喩は避けてください。",
            "具体的な人物・動作・空気を1つだけ拾ってください。",
        ),
    ),
    "romance": StoryStyleProfile(
        story_id="romance",
        dialogue_mode="romantic_drama",
        narration_mode="atmospheric",
        narration_min_interval_turns=3,
        narration_emotion_threshold=0.35,
        allow_literary_narration=True,
        quality_guard_mode="default",
        dialogue_rules=(
            "具体的な相手・感情・行動を優先してください。",
            "発言は1〜2文を基本にしてください。",
            "感情や想いを正直に、でも押しつけがましくなく出してください。",
            "過度に詩的な表現は避けてください。",
        ),
        narration_rules=(
            "場の空気・二人の距離感・小さな感情の揺れを短く伝えてください。",
            "1〜2文に収めてください。",
            "雰囲気は大切に、でも冗長にならないように。",
            "具体的な人物・仕草・視線を1つだけ拾ってください。",
        ),
    ),
    "action": StoryStyleProfile(
        story_id="action",
        dialogue_mode="action_drama",
        narration_mode="minimal_action",
        narration_min_interval_turns=2,
        narration_emotion_threshold=0.45,
        allow_literary_narration=False,
        quality_guard_mode="default",
        dialogue_rules=(
            "具体的な相手・行動・決断を優先してください。",
            "発言は1〜2文を基本にしてください。",
            "短く決断して動いてください。",
            "長い説明や感傷は避けてください。",
        ),
        narration_rules=(
            "動きと緊張を短く伝えてください。",
            "1〜2文に収めてください。",
            "比喩は最小限に。",
            "具体的な行動・音・動作を1つだけ拾ってください。",
        ),
    ),
}

VALID_STORY_MODES: frozenset[str] = frozenset(_STYLE_PROFILES_BY_MODE.keys())


def get_story_style_profile_by_mode(story_mode: str) -> StoryStyleProfile:
    """story_mode 文字列からプロファイルを返す。未知のモードは DEFAULT を返す。"""
    return _STYLE_PROFILES_BY_MODE.get(str(story_mode or "").strip(), DEFAULT_STYLE_PROFILE)


def get_story_style_profile(story_id: str) -> StoryStyleProfile:
    """後方互換 API。story_id ベースの呼び出しは DEFAULT を返す。
    呼び出し元は get_story_style_profile_by_mode() への移行を推奨。
    """
    return DEFAULT_STYLE_PROFILE

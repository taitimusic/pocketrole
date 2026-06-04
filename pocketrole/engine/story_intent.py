"""engine/story_intent.py - internal story intent profiles and review heuristics."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any


@dataclass(frozen=True)
class StoryIntentProfile:
    """Runtime-only story intent profile."""

    story_id: str
    primary_mode: str = "default"
    preferred_tension_types: tuple[str, ...] = ()
    preferred_intervention_types: tuple[str, ...] = ()
    preferred_pattern_types: tuple[str, ...] = ()
    episode_tempo: str = "moderate"
    deprioritized_tension_types: tuple[str, ...] = ()
    deprioritized_intervention_types: tuple[str, ...] = ()
    deprioritized_pattern_types: tuple[str, ...] = ()
    review_expectations: tuple[str, ...] = ()


DEFAULT_INTENT_PROFILE = StoryIntentProfile(
    story_id="default",
    review_expectations=("core quality metrics should remain stable",),
)

_INTENT_PROFILES = {
    "ankoku_gakuen": StoryIntentProfile(
        story_id="ankoku_gakuen",
        primary_mode="mystery_drama",
        preferred_tension_types=("mystery", "conflict", "revelation"),
        preferred_intervention_types=("revelation", "relationship_catalyst", "opportunity"),
        preferred_pattern_types=(
            "mystery",
            "near_reveal",
            "misunderstanding",
            "conflict",
        ),
        episode_tempo="moderate",
        deprioritized_tension_types=("moral_dilemma", "rivalry"),
        deprioritized_pattern_types=("status_clash", "bluff_or_showoff", "small_win_loss"),
        deprioritized_intervention_types=("crisis",),
        review_expectations=(
            "mystery and revelation patterns should drive scene progression",
            "near_reveal hooks should accumulate and resolve gradually",
            "secrets should be the primary source of tension",
        ),
    ),
}

# story_mode ベースのプロファイルマップ
# world_config.yaml の story.story_mode に対応する intent profile を提供する。
_INTENT_PROFILES_BY_MODE: dict[str, StoryIntentProfile] = {
    "drama": StoryIntentProfile(
        story_id="drama",
        primary_mode="mystery_drama",
        preferred_tension_types=("mystery", "conflict", "revelation"),
        preferred_intervention_types=("revelation", "relationship_catalyst", "opportunity"),
        preferred_pattern_types=(
            "mystery",
            "near_reveal",
            "misunderstanding",
            "conflict",
        ),
        episode_tempo="moderate",
        deprioritized_tension_types=("moral_dilemma", "rivalry"),
        deprioritized_pattern_types=("status_clash", "bluff_or_showoff", "small_win_loss"),
        deprioritized_intervention_types=("crisis",),
        review_expectations=(
            "mystery and revelation patterns should drive scene progression",
            "near_reveal hooks should accumulate and resolve gradually",
            "secrets should be the primary source of tension",
        ),
    ),
    "comedy": StoryIntentProfile(
        story_id="comedy",
        primary_mode="comic_ensemble",
        preferred_tension_types=("conflict", "rivalry", "mystery"),
        preferred_intervention_types=("relationship_catalyst", "opportunity", "revelation"),
        preferred_pattern_types=(
            "misunderstanding",
            "status_clash",
            "small_win_loss",
            "role_reversal",
            "bluff_or_showoff",
        ),
        episode_tempo="fast",
        deprioritized_tension_types=("moral_dilemma", "betrayal"),
        deprioritized_intervention_types=("crisis",),
        review_expectations=(
            "reply-driven exchanges should dominate over monologue-heavy turns",
            "hooks should recycle into quick scene-scale incidents",
            "tensions should cluster around conflict, rivalry, or mystery",
        ),
    ),
    "romance": StoryIntentProfile(
        story_id="romance",
        primary_mode="romantic_drama",
        preferred_tension_types=("mystery", "conflict", "romantic"),
        preferred_intervention_types=("revelation", "relationship_catalyst", "opportunity"),
        preferred_pattern_types=(
            "near_reveal",
            "misunderstanding",
            "mystery",
            "conflict",
        ),
        episode_tempo="moderate",
        deprioritized_tension_types=("moral_dilemma",),
        deprioritized_pattern_types=("status_clash", "bluff_or_showoff"),
        review_expectations=(
            "emotional revelations and near_reveal hooks should drive scene progression",
            "misunderstandings should resolve gradually with emotional payoff",
        ),
    ),
    "action": StoryIntentProfile(
        story_id="action",
        primary_mode="action_drama",
        preferred_tension_types=("conflict", "mystery", "revelation"),
        preferred_intervention_types=("opportunity", "revelation", "crisis"),
        preferred_pattern_types=(
            "conflict",
            "small_win_loss",
            "near_reveal",
            "misunderstanding",
        ),
        episode_tempo="fast",
        deprioritized_tension_types=("moral_dilemma",),
        deprioritized_pattern_types=("bluff_or_showoff",),
        review_expectations=(
            "direct conflict patterns should drive scene progression",
            "quick resolution and action beats should dominate",
        ),
    ),
}


@lru_cache(maxsize=None)
def get_story_intent_profile(story_id: str) -> StoryIntentProfile:
    """後方互換 API。story_id ベースの呼び出しは story_id 固有プロファイルか DEFAULT を返す。
    呼び出し元は get_story_intent_profile_by_mode() への移行を推奨。
    """
    return _INTENT_PROFILES.get(story_id, DEFAULT_INTENT_PROFILE)


def get_story_intent_profile_by_mode(story_mode: str) -> StoryIntentProfile:
    """story_mode 文字列から intent profile を返す。未知のモードは DEFAULT を返す。"""
    return _INTENT_PROFILES_BY_MODE.get(str(story_mode or "").strip(), DEFAULT_INTENT_PROFILE)


def detect_story_intent_gaps(
    profile: StoryIntentProfile,
    snapshot: dict[str, Any],
) -> list[tuple[str, str]]:
    """Return lightweight review-only intent alignment gaps."""
    if profile.primary_mode != "comic_ensemble":
        return []

    reply_ratio = float(snapshot.get("reply_ratio", 0.0))
    monologue_ratio = float(snapshot.get("monologue_ratio", 0.0))
    open_hooks = int(snapshot.get("open_hooks", 0))
    hooks_resolved_recent = int(snapshot.get("hooks_resolved_recent", 0))
    active_patterns = int(snapshot.get("active_interaction_patterns", 0))
    dominant_pattern_types = set(str(pattern_type) for pattern_type in snapshot.get("dominant_pattern_types", []) or [])

    gaps: list[tuple[str, str]] = []
    if reply_ratio < 0.20 and monologue_ratio >= 0.30:
        gaps.append(
            (
                "story_intent_misaligned",
                "comic_ensemble の狙いに対して独演寄りで、短い応酬の連鎖が不足している。",
            )
        )
    elif reply_ratio < 0.20:
        gaps.append(
            (
                "story_intent_misaligned",
                "comic_ensemble の狙いに対して独演寄りで、往復会話と掛け合いの推進力が足りない。",
            )
        )
    if open_hooks > 0 and hooks_resolved_recent == 0:
        gaps.append(
            (
                "story_intent_hook_stalled",
                "comic_ensemble の狙いに対して open hook の回収が弱く、小事件の回転が止まりやすい。",
            )
        )
    if active_patterns > 0 and dominant_pattern_types & {"status_clash", "small_win_loss"} and not (
        dominant_pattern_types & {"role_reversal", "bluff_or_showoff"}
    ):
        gaps.append(
            (
                "story_intent_pattern_mix_flat",
                "comic_ensemble の狙いに対して張り合い系 pattern はあるが、role_reversal / bluff_or_showoff が無く、展開の崩しが足りない。",
            )
        )
    return gaps

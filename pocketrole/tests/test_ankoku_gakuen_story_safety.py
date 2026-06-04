"""Safety checks for bundled ankoku_gakuen sample story text."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
STORY_IDS = ("ankoku_gakuen", "ankoku_gakuen_2")
BANNED_STORY_TERMS = (
    "中卒",
    "カイジ",
    "ナニワ金融道",
    "ざわ",
)


def _story_yaml_text(story_id: str) -> str:
    story_dir = ROOT / "stories" / story_id
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(story_dir.glob("*.yaml"))
    )


def test_ankoku_gakuen_samples_do_not_include_sensitive_or_copyright_references() -> None:
    for story_id in STORY_IDS:
        text = _story_yaml_text(story_id)

        for term in BANNED_STORY_TERMS:
            assert term not in text, f"{story_id} still contains {term!r}"


def test_ankoku_gakuen_samples_use_soft_attendance_gag() -> None:
    for story_id in STORY_IDS:
        text = _story_yaml_text(story_id)

        assert "出席日数" in text
        assert "出席日数どうなるの" in text

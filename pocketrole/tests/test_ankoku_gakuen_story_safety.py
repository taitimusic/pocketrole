"""Safety checks for bundled ankoku_gakuen sample story text."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
STORY_IDS = ("ankoku_gakuen", "ankoku_gakuen_2", "ankoku_gakuen_mystery")
PUBLIC_TEXT_PATHS = (
    ROOT / "engine" / "story_engine.py",
)
RESTRICTED_REFERENCE_TERM_CODEPOINTS = (
    (0x5B66, 0x6B74),
    (0x4E2D, 0x5352),
    (0x30AB, 0x30A4, 0x30B8),
    (0x30CA, 0x30CB, 0x30EF, 0x91D1, 0x878D, 0x9053),
    (0x3056, 0x308F),
    (0x9280, 0x20, 0x9054, 0x4E4B, 0x52A9),
    (0x9280, 0x3068, 0x91D1),
    (0x5229, 0x6839, 0x5DDD),
    (0x4F0A, 0x85E4, 0x958B, 0x53F8),
    (0x685C, 0x6728),
    (0x5CF0, 0x85E4),
    (0x30B9, 0x30E9, 0x30E0, 0x30C0, 0x30F3, 0x30AF),
)


def _term_from_codepoints(codepoints: tuple[int, ...]) -> str:
    return "".join(chr(codepoint) for codepoint in codepoints)


BANNED_STORY_TERMS = tuple(
    _term_from_codepoints(codepoints) for codepoints in RESTRICTED_REFERENCE_TERM_CODEPOINTS
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
            if term in text:
                raise AssertionError(f"{story_id} contains a restricted reference term")


def test_safety_check_source_does_not_expose_restricted_reference_terms() -> None:
    source = Path(__file__).read_text(encoding="utf-8")

    for codepoints in RESTRICTED_REFERENCE_TERM_CODEPOINTS:
        term = _term_from_codepoints(codepoints)
        assert term not in source


def test_public_text_sources_do_not_expose_restricted_reference_terms() -> None:
    for path in PUBLIC_TEXT_PATHS:
        text = path.read_text(encoding="utf-8")

        for term in BANNED_STORY_TERMS:
            if term in text:
                raise AssertionError(f"{path.relative_to(ROOT)} contains a restricted reference term")


def test_ankoku_gakuen_samples_use_soft_attendance_gag() -> None:
    for story_id in ("ankoku_gakuen", "ankoku_gakuen_2"):
        text = _story_yaml_text(story_id)

        assert "出席日数" in text
        assert "出席日数どうなるの" in text

"""Tests for engine/structured_values.py."""

from __future__ import annotations

from engine.structured_values import normalize_unit_score


def test_normalize_unit_score_handles_percent_labels_and_invalid_values() -> None:
    assert normalize_unit_score("82%") == 0.82
    assert normalize_unit_score("very_high") == 0.9
    assert normalize_unit_score("not_sure", default=0.5) == 0.5
    assert normalize_unit_score(1.7) == 1.0

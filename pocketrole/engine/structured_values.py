"""Helpers for normalizing structured parser values."""

from __future__ import annotations

from typing import Any

_QUALITATIVE_UNIT_SCORES = {
    "very_low": 0.15,
    "low": 0.30,
    "medium": 0.50,
    "mid": 0.50,
    "moderate": 0.50,
    "high": 0.75,
    "very_high": 0.90,
    "とても低い": 0.15,
    "低い": 0.30,
    "普通": 0.50,
    "中": 0.50,
    "高い": 0.75,
    "とても高い": 0.90,
}


def normalize_unit_score(value: Any, *, default: float = 0.5) -> float:
    """Normalize mixed score tokens into a clamped 0.0-1.0 float."""
    if isinstance(value, bool):
        return _clamp_unit(default)
    if isinstance(value, (int, float)):
        return _clamp_unit(float(value))
    if not isinstance(value, str):
        return _clamp_unit(default)

    cleaned = value.strip().lower()
    if not cleaned:
        return _clamp_unit(default)

    if cleaned.endswith("%"):
        try:
            return _clamp_unit(float(cleaned[:-1].strip()) / 100.0)
        except ValueError:
            return _clamp_unit(default)

    qualitative = _QUALITATIVE_UNIT_SCORES.get(cleaned)
    if qualitative is not None:
        return qualitative

    try:
        return _clamp_unit(float(cleaned))
    except ValueError:
        return _clamp_unit(default)


def _clamp_unit(value: float) -> float:
    return max(0.0, min(1.0, value))

"""character overlay export/apply helpers."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

FIELD_REGISTRY = {
    "current_goal": ("goal",),
    "current_worry": ("worry",),
    "personality_core": ("personality", "type"),
}


def build_canonical_patch(
    db_overlay: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """DB overlay を canonical patch と unmapped overlay に分ける。"""
    canonical_patch: dict[str, Any] = {}
    unmapped: dict[str, Any] = {}
    for key, value in db_overlay.items():
        path = FIELD_REGISTRY.get(key)
        if path is None:
            unmapped[key] = value
            continue
        _set_nested(canonical_patch, path, value)
    return canonical_patch, unmapped


def _set_nested(target: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node = target
    for part in path[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[path[-1]] = value


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """dict 同士の深い merge を返す。"""
    merged = deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged

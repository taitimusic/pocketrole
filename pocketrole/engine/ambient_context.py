"""engine/ambient_context.py — 短期環境コンテキスト管理。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from engine.config import AmbientContextConfig

if TYPE_CHECKING:
    from db.db_manager import DatabaseManager
    from engine.llm.router import LLMRouter


_BODY_CATALOG: dict[str, dict[str, Any]] = {
    "hunger": {
        "summary": "少し空腹を感じている。",
        "emotion_delta": {"stress": 0.06, "motivation": -0.04},
    },
    "fatigue": {
        "summary": "少し疲れが溜まっている。",
        "emotion_delta": {"stress": 0.05, "motivation": -0.06},
    },
    "thermal_discomfort": {
        "summary": "暑さか寒さが少し気になっている。",
        "emotion_delta": {"stress": 0.04},
    },
}

_PLACE_CATALOG: dict[str, dict[str, Any]] = {
    "crowding": {
        "summary": "{place_name}は少し人の気配が多い。",
        "emotion_delta": {"stress": 0.04, "excitement": 0.03},
    },
    "noise": {
        "summary": "{place_name}の周囲が少し騒がしい。",
        "emotion_delta": {"stress": 0.07},
    },
    "stale_air": {
        "summary": "{place_name}の空気が少しこもっている。",
        "emotion_delta": {"stress": 0.04, "motivation": -0.03},
    },
    "small_interrupt": {
        "summary": "近くで小さな物音や気配が続いている。",
        "emotion_delta": {"excitement": 0.05},
    },
}


@dataclass(frozen=True)
class AmbientTurnContext:
    body_state_texts: list[str] = field(default_factory=list)
    ambient_texts: list[str] = field(default_factory=list)
    emotion_delta: dict[str, float] = field(default_factory=dict)
    active_factor_ids: list[int] = field(default_factory=list)


class AmbientContextManager:
    """短期持続の身体感覚・場所ノイズを管理する。"""

    def __init__(
        self,
        story_id: str,
        db: DatabaseManager,
        llm_router: LLMRouter,
        config: AmbientContextConfig,
        llm_provider: str = "",
        llm_model: str = "",
    ) -> None:
        self.story_id = story_id
        self._db = db
        self._llm_router = llm_router
        self._config = config
        self._llm_provider = llm_provider
        self._llm_model = llm_model

    async def resolve_turn(
        self,
        *,
        turn_number: int,
        sim_datetime: str,
        char_id: str,
        place_id: str,
        place_name: str,
        same_place_char_ids: list[str],
        emotions: dict[str, float],
        move_reason: str | None,
        anomaly: dict[str, Any] | None,
    ) -> AmbientTurnContext:
        if not self._config.enabled:
            return AmbientTurnContext()

        active_rows = await self._db.get_active_ambient_states(
            self.story_id,
            turn_number,
            [("char", char_id), ("place", place_id)],
        )
        body_rows = [row for row in active_rows if row["scope_type"] == "char"]
        place_rows = [row for row in active_rows if row["scope_type"] == "place"]

        if len(body_rows) < self._config.max_active_body_factors:
            body_factor = self._choose_body_factor(sim_datetime, emotions)
            if body_factor is not None:
                factor_id = await self._db.insert_ambient_state(
                    self.story_id,
                    self._make_state_payload(
                        scope_type="char",
                        scope_id=char_id,
                        factor_kind="body",
                        factor_key=body_factor,
                        turn_number=turn_number,
                        summary=_BODY_CATALOG[body_factor]["summary"],
                        emotion_delta=_BODY_CATALOG[body_factor]["emotion_delta"],
                    ),
                )
                body_rows.append(
                    {
                        "id": factor_id,
                        "summary": _BODY_CATALOG[body_factor]["summary"],
                        "emotion_delta": _BODY_CATALOG[body_factor]["emotion_delta"],
                    }
                )

        if len(place_rows) < self._config.max_active_place_factors:
            place_factor = self._choose_place_factor(place_name, same_place_char_ids, anomaly, move_reason)
            if place_factor is not None:
                summary = _PLACE_CATALOG[place_factor]["summary"].format(place_name=place_name)
                factor_id = await self._db.insert_ambient_state(
                    self.story_id,
                    self._make_state_payload(
                        scope_type="place",
                        scope_id=place_id,
                        factor_kind="place",
                        factor_key=place_factor,
                        turn_number=turn_number,
                        summary=summary,
                        emotion_delta=_PLACE_CATALOG[place_factor]["emotion_delta"],
                    ),
                )
                place_rows.append(
                    {
                        "id": factor_id,
                        "summary": summary,
                        "emotion_delta": _PLACE_CATALOG[place_factor]["emotion_delta"],
                    }
                )

        body_state_texts = [row["summary"] for row in body_rows[: self._config.max_prompt_items]]
        remaining = max(self._config.max_prompt_items - len(body_state_texts), 0)
        ambient_texts = [row["summary"] for row in place_rows[:remaining]]

        emotion_delta: dict[str, float] = {}
        for row in body_rows + place_rows:
            for key, value in row.get("emotion_delta", {}).items():
                emotion_delta[key] = emotion_delta.get(key, 0.0) + float(value)

        active_factor_ids = [int(row["id"]) for row in body_rows + place_rows if row.get("id") is not None]
        return AmbientTurnContext(
            body_state_texts=body_state_texts,
            ambient_texts=ambient_texts,
            emotion_delta=emotion_delta,
            active_factor_ids=active_factor_ids,
        )

    def _choose_body_factor(self, sim_datetime: str, emotions: dict[str, float]) -> str | None:
        hour = int(sim_datetime[11:13])
        if 11 <= hour <= 13:
            return "hunger"
        if hour >= 18 or emotions.get("motivation", 0.5) < 0.45:
            return "fatigue"
        if emotions.get("stress", 0.5) > 0.65:
            return "thermal_discomfort"
        return "hunger"

    def _choose_place_factor(
        self,
        place_name: str,
        same_place_char_ids: list[str],
        anomaly: dict[str, Any] | None,
        move_reason: str | None,
    ) -> str | None:
        if anomaly is not None:
            return "small_interrupt"
        if len(same_place_char_ids) >= 1:
            return "crowding"
        if "廊下" in place_name or "教室" in place_name:
            return "noise"
        if move_reason:
            return "stale_air"
        return "noise"

    def _make_state_payload(
        self,
        *,
        scope_type: str,
        scope_id: str,
        factor_kind: str,
        factor_key: str,
        turn_number: int,
        summary: str,
        emotion_delta: dict[str, float],
    ) -> dict[str, Any]:
        duration = max(self._config.min_duration_turns, min(self._config.max_duration_turns, 5))
        return {
            "scope_type": scope_type,
            "scope_id": scope_id,
            "factor_kind": factor_kind,
            "factor_key": factor_key,
            "summary": summary,
            "intensity": 0.4,
            "emotion_delta": emotion_delta,
            "created_turn": turn_number,
            "expires_turn": turn_number + duration,
            "last_applied_turn": turn_number,
            "source": "template",
        }

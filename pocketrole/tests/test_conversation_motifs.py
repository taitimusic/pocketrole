from __future__ import annotations

from typing import Any

import pytest

from engine.conversation_motifs import ConversationMotifEngine


class FakeMotifDB:
    def __init__(self) -> None:
        self.settings = [
            {
                "motif_id": "solo_seed_rondo",
                "enabled": True,
                "strength": "moderate",
                "cooldown_turns": 4,
            }
        ]
        self.open_hooks: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = []
        self.updated: list[tuple[int, dict[str, Any]]] = []

    async def get_conversation_motif_settings(self, story_id: str) -> list[dict[str, Any]]:
        return self.settings

    async def get_open_story_hooks(self, story_id: str) -> list[dict[str, Any]]:
        return self.open_hooks

    async def find_active_conversation_motif_run_by_seed_hook(
        self,
        story_id: str,
        seed_hook_id: int,
    ) -> dict[str, Any] | None:
        for run in self.runs:
            if run.get("seed_hook_id") == seed_hook_id and run.get("status") == "active":
                return run
        return None

    async def insert_conversation_motif_run(
        self,
        story_id: str,
        run: dict[str, Any],
    ) -> int:
        run_id = len(self.runs) + 1
        self.runs.append({"id": run_id, "story_id": story_id, **run})
        return run_id

    async def get_active_conversation_motif_runs(self, story_id: str) -> list[dict[str, Any]]:
        return [run for run in self.runs if run.get("status") == "active"]

    async def update_conversation_motif_run(self, run_id: int, patch: dict[str, Any]) -> None:
        self.updated.append((run_id, patch))
        for run in self.runs:
            if run["id"] == run_id:
                run.update(patch)
                return


@pytest.mark.asyncio
async def test_process_hooks_starts_solo_seed_rondo_from_open_solo_seed() -> None:
    db = FakeMotifDB()
    db.open_hooks = [
        {
            "id": 10,
            "hook_type": "solo_seed",
            "status": "open",
            "owner_char_id": "miritia",
            "target_char_id": None,
            "title": "聞き逃せない独り言",
            "description": "あのテープ、まだ地下倉庫にある気がする。",
            "source_log_id": 77,
            "source_scene_id": 5,
        }
    ]
    engine = ConversationMotifEngine(db=db, story_id="ankoku_gakuen")

    await engine.process_hooks(turn_number=21)

    assert len(db.runs) == 1
    assert db.runs[0]["motif_id"] == "solo_seed_rondo"
    assert db.runs[0]["stage"] == "seeded"
    assert db.runs[0]["seed_hook_id"] == 10
    assert db.runs[0]["owner_char_id"] == "miritia"


@pytest.mark.asyncio
async def test_apply_turn_context_asks_pickup_character_to_reply_to_solo_seed_owner() -> None:
    db = FakeMotifDB()
    db.runs = [
        {
            "id": 1,
            "story_id": "ankoku_gakuen",
            "motif_id": "solo_seed_rondo",
            "status": "active",
            "stage": "seeded",
            "owner_char_id": "miritia",
            "pickup_char_id": None,
            "place_id": "music_room",
            "description": "あのテープ、まだ地下倉庫にある気がする。",
            "started_turn": 21,
            "last_advanced_turn": 21,
            "cooldown_until_turn": None,
        }
    ]
    engine = ConversationMotifEngine(db=db, story_id="ankoku_gakuen")

    context = await engine.apply_turn_context(
        char_id="chururun",
        place_id="music_room",
        participant_ids=["miritia", "chururun"],
        turn_number=22,
    )

    assert context is not None
    assert context["msg_type"] == "reply"
    assert context["target_char_id"] == "miritia"
    assert context["target_char_name_source"] == "miritia"
    assert "地下倉庫" in context["motif_text"]
    assert db.runs[0]["stage"] == "picked_up"
    assert db.runs[0]["pickup_char_id"] == "chururun"

"""Conversation motif runtime helpers.

会話モチーフは、台詞内容を決めるのではなく「誰が誰の言葉を拾うか」だけを
弱く補助する薄い制御層である。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from db.db_manager import DatabaseManager


SOLO_SEED_RONDO = "solo_seed_rondo"


class ConversationMotifEngine:
    """Manage built-in conversation motif runs for one story."""

    def __init__(self, db: "DatabaseManager | Any", story_id: str) -> None:
        self._db = db
        self._story_id = story_id

    async def process_hooks(self, turn_number: int) -> None:
        """Create motif runs from eligible open hooks."""
        if not await self._is_enabled(SOLO_SEED_RONDO):
            return
        hooks = await self._db.get_open_story_hooks(self._story_id)
        for hook in hooks:
            if str(hook.get("hook_type") or "") != "solo_seed":
                continue
            hook_id = hook.get("id")
            if hook_id is None:
                continue
            owner_char_id = str(hook.get("owner_char_id") or "").strip()
            description = str(hook.get("description") or "").strip()
            if not owner_char_id or not description:
                continue
            existing = await self._db.find_active_conversation_motif_run_by_seed_hook(
                self._story_id,
                int(hook_id),
            )
            if existing is not None:
                continue
            place_id = await self._resolve_hook_place_id(hook)
            await self._db.insert_conversation_motif_run(
                self._story_id,
                {
                    "motif_id": SOLO_SEED_RONDO,
                    "status": "active",
                    "stage": "seeded",
                    "seed_hook_id": int(hook_id),
                    "seed_log_id": hook.get("source_log_id"),
                    "owner_char_id": owner_char_id,
                    "pickup_char_id": None,
                    "place_id": place_id,
                    "title": hook.get("title") or "聞き逃せない独り言",
                    "description": description,
                    "started_turn": turn_number,
                    "last_advanced_turn": turn_number,
                    "cooldown_until_turn": None,
                },
            )

    async def apply_turn_context(
        self,
        *,
        char_id: str,
        place_id: str,
        participant_ids: list[str],
        turn_number: int,
    ) -> dict[str, Any] | None:
        """Return a weak turn override if a motif should guide this speaker."""
        setting = await self._setting(SOLO_SEED_RONDO)
        if not setting.get("enabled"):
            return None
        if len(participant_ids) < 2:
            return None
        participant_set = set(participant_ids)
        for run in await self._db.get_active_conversation_motif_runs(self._story_id):
            if str(run.get("motif_id") or "") != SOLO_SEED_RONDO:
                continue
            if run.get("cooldown_until_turn") is not None and int(run["cooldown_until_turn"]) > turn_number:
                continue
            run_place = str(run.get("place_id") or "").strip()
            if run_place and run_place != place_id:
                continue
            owner_char_id = str(run.get("owner_char_id") or "").strip()
            if not owner_char_id or owner_char_id not in participant_set:
                continue
            stage = str(run.get("stage") or "seeded")
            pickup_char_id = str(run.get("pickup_char_id") or "").strip() or None
            if stage == "seeded":
                if char_id == owner_char_id:
                    continue
                if pickup_char_id is not None and pickup_char_id != char_id:
                    continue
                return await self._advance(
                    run,
                    stage="picked_up",
                    char_id=char_id,
                    target_char_id=owner_char_id,
                    turn_number=turn_number,
                    pickup_char_id=pickup_char_id or char_id,
                )
            if stage == "picked_up":
                if char_id != owner_char_id:
                    continue
                target_char_id = pickup_char_id or self._first_other(participant_ids, owner_char_id)
                if target_char_id is None:
                    continue
                return await self._advance(
                    run,
                    stage="deepened",
                    char_id=char_id,
                    target_char_id=target_char_id,
                    turn_number=turn_number,
                )
            if stage == "deepened":
                if char_id == owner_char_id:
                    continue
                target_char_id = owner_char_id
                return await self._advance(
                    run,
                    stage="chain",
                    char_id=char_id,
                    target_char_id=target_char_id,
                    turn_number=turn_number,
                )
            if stage == "chain":
                await self._db.update_conversation_motif_run(
                    int(run["id"]),
                    {
                        "status": "cooldown",
                        "stage": "cooldown",
                        "last_advanced_turn": turn_number,
                        "cooldown_until_turn": turn_number + int(setting.get("cooldown_turns") or 0),
                    },
                )
        return None

    async def _advance(
        self,
        run: dict[str, Any],
        *,
        stage: str,
        char_id: str,
        target_char_id: str,
        turn_number: int,
        pickup_char_id: str | None = None,
    ) -> dict[str, Any]:
        patch: dict[str, Any] = {
            "stage": stage,
            "last_advanced_turn": turn_number,
        }
        if pickup_char_id is not None:
            patch["pickup_char_id"] = pickup_char_id
        await self._db.update_conversation_motif_run(int(run["id"]), patch)
        return {
            "msg_type": "reply",
            "target_char_id": target_char_id,
            "target_char_name_source": target_char_id,
            "reply_to_log_id": run.get("seed_log_id"),
            "motif_text": self._format_motif_text(run, stage=stage, char_id=char_id),
        }

    def _format_motif_text(
        self,
        run: dict[str, Any],
        *,
        stage: str,
        char_id: str,
    ) -> str:
        description = str(run.get("description") or "").strip()
        if stage == "picked_up":
            return f"さっき聞こえた独り言: {description}。気になったところへ自然に声をかける。"
        if stage == "deepened":
            return f"さっき拾われた言葉: {description}。自分の言葉として少しだけ続きを出す。"
        return f"会話の種: {description}。その場の誰かの反応として自然に受ける。"

    async def _is_enabled(self, motif_id: str) -> bool:
        return bool((await self._setting(motif_id)).get("enabled"))

    async def _setting(self, motif_id: str) -> dict[str, Any]:
        for item in await self._db.get_conversation_motif_settings(self._story_id):
            if item.get("motif_id") == motif_id:
                return item
        return {"motif_id": motif_id, "enabled": False, "cooldown_turns": 4}

    async def _resolve_hook_place_id(self, hook: dict[str, Any]) -> str | None:
        direct_place = str(hook.get("place_id") or "").strip()
        if direct_place:
            return direct_place
        scene_id = hook.get("source_scene_id")
        get_story_scene = getattr(self._db, "get_story_scene", None)
        if scene_id is None or not callable(get_story_scene):
            return None
        scene = await get_story_scene(self._story_id, int(scene_id))
        if not isinstance(scene, dict):
            return None
        place_id = str(scene.get("place_id") or "").strip()
        return place_id or None

    @staticmethod
    def _first_other(participant_ids: list[str], char_id: str) -> str | None:
        for participant_id in participant_ids:
            if participant_id != char_id:
                return participant_id
        return None

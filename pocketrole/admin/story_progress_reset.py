"""Story progress reset service for the admin surface."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import aiohttp

from db.db_manager import DatabaseManager
from engine.config import StoryWebPostTargetConfig
from engine.news_url import PublicHttpUrlValidationError, validate_public_http_url


class StoryProgressResetError(RuntimeError):
    """Raised when story progress reset cannot be completed."""


RemoteResetSender = Callable[[StoryWebPostTargetConfig, str], Awaitable[dict[str, object]]]


async def send_remote_story_reset(
    target: StoryWebPostTargetConfig,
    story_id: str,
) -> dict[str, object]:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            target.receiver_url,
            json={
                "auth_token": target.auth_token,
                "story_id": story_id,
                "action": "reset_story",
                "confirm": True,
            },
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            if response.status >= 400:
                body = await response.text()
                raise StoryProgressResetError(
                    f"Remote public log reset failed: HTTP {response.status} {body[:160]}"
                )
            try:
                payload: Any = await response.json()
            except Exception as exc:  # pragma: no cover - defensive for hosted PHP errors
                raise StoryProgressResetError("Remote public log reset returned invalid JSON") from exc
            if not isinstance(payload, dict) or payload.get("status") != "ok":
                raise StoryProgressResetError("Remote public log reset did not return ok")
            return dict(payload)


class StoryProgressResetService:
    """Reset runtime progress while preserving story definitions and assets."""

    def __init__(
        self,
        *,
        db: DatabaseManager,
        chatlog_root: str | Path,
        web_post_targets: Mapping[str, StoryWebPostTargetConfig] | None = None,
        remote_reset_sender: RemoteResetSender | None = None,
    ) -> None:
        self._db = db
        self._chatlog_root = Path(chatlog_root)
        self._web_post_targets = dict(web_post_targets or {})
        self._remote_reset_sender = remote_reset_sender or send_remote_story_reset

    async def reset(self, story_id: str) -> dict[str, object]:
        remote_public_logs_reset = await self._reset_remote_public_logs(story_id)
        db_counts = await self._db.reset_story_progress(story_id)
        public_logs_deleted = self._delete_public_dat_logs(story_id)
        return {
            "story_id": story_id,
            "db_counts": db_counts,
            "public_logs_deleted": public_logs_deleted,
            "remote_public_logs_reset": remote_public_logs_reset,
        }

    async def _resolve_web_post_target(
        self, story_id: str
    ) -> StoryWebPostTargetConfig | None:
        """DB 優先で Web 投稿先ターゲットを解決する。

        解決順: per-story DB → グローバルデフォルト DB → YAML フォールバック。
        """
        story = await self._db.get_story(story_id)
        url   = ((story or {}).get("web_receiver_url") or "").strip()
        token = ((story or {}).get("web_auth_token")   or "").strip()

        if not url:
            url   = (await self._db.get_system_setting("default_web_receiver_url") or "").strip()
        if not token:
            token = (await self._db.get_system_setting("default_web_auth_token")   or "").strip()

        if url and token:
            try:
                url = validate_public_http_url(url)
            except PublicHttpUrlValidationError as exc:
                raise StoryProgressResetError(f"Invalid remote public log reset URL: {exc}") from exc
            return StoryWebPostTargetConfig(enabled=True, receiver_url=url, auth_token=token)

        # YAML フォールバック（後方互換）
        return self._web_post_targets.get(story_id)

    async def reset_remote_public_logs(self, story_id: str) -> dict[str, object]:
        target = await self._resolve_web_post_target(story_id)
        if target is None:
            return {"status": "skipped", "reason": "not_configured"}
        if not target.enabled:
            return {"status": "skipped", "reason": "disabled"}
        if not target.receiver_url or not target.auth_token:
            raise StoryProgressResetError("Remote public log reset target is incomplete")
        try:
            target = StoryWebPostTargetConfig(
                enabled=target.enabled,
                receiver_url=validate_public_http_url(target.receiver_url),
                auth_token=target.auth_token,
            )
        except PublicHttpUrlValidationError as exc:
            raise StoryProgressResetError(f"Invalid remote public log reset URL: {exc}") from exc
        try:
            return await self._remote_reset_sender(target, story_id)
        except StoryProgressResetError:
            raise
        except Exception as exc:
            raise StoryProgressResetError(str(exc)) from exc

    async def _reset_remote_public_logs(self, story_id: str) -> dict[str, object]:
        return await self.reset_remote_public_logs(story_id)

    def _delete_public_dat_logs(self, story_id: str) -> int:
        root = self._chatlog_root.resolve()
        story_dir = (root / story_id).resolve()
        try:
            story_dir.relative_to(root)
        except ValueError as exc:
            raise StoryProgressResetError("Invalid story log directory") from exc

        if not story_dir.exists():
            return 0
        if not story_dir.is_dir():
            raise StoryProgressResetError("Story log path is not a directory")

        deleted = 0
        for path in story_dir.glob("*.dat"):
            if not path.is_file():
                continue
            path.unlink()
            deleted += 1
        return deleted

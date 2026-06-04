"""engine/web_poster.py — receiver.php へのバッチ HTTP POST ワーカー。

- DB の chat_logs（posted_to_web=0）を batch_size 件ずつ POST
- 連続失敗 max_consecutive_failures 回で pause_duration_sec 秒一時停止
- run() を asyncio.Task として起動し、stop() でグレースフル停止
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp

from db.db_manager import DatabaseManager
from engine.config import WebPosterConfig

logger = logging.getLogger(__name__)


class WebPoster:
    """receiver.php へのバッチ送信ワーカー。

    - DB の chat_logs（posted_to_web=0）を batch_size 件ずつ POST
    - 連続失敗 max_consecutive_failures 回で pause_duration_sec 秒一時停止
    - run() を asyncio.Task として起動し、stop() でグレースフル停止
    """

    def __init__(
        self,
        story_id: str,
        config: WebPosterConfig,
        db: DatabaseManager,
    ) -> None:
        self._story_id = story_id
        self._config = config
        self._db = db
        self._running: bool = False
        self._consecutive_failures: int = 0
        self._paused_until: float | None = None  # time.monotonic() 基準
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        """aiohttp.ClientSession を生成する。run() 前に呼ぶ。"""
        self._session = aiohttp.ClientSession()
        logger.info("WebPoster session created", extra={"story_id": self._story_id})

    async def run(self) -> None:
        """バッチ送信メインループ。stop() が呼ばれるまで実行する。"""
        self._running = True
        logger.info("WebPoster started", extra={"story_id": self._story_id})
        while self._running:
            if self._is_paused():
                await asyncio.sleep(5)
                continue
            try:
                had_logs = await self._send_batch()
                self._consecutive_failures = 0
                if not had_logs:
                    await asyncio.sleep(self._config.retry_interval_sec)
            except Exception as exc:
                self._consecutive_failures += 1
                logger.warning(
                    "WebPoster send failed (consecutive: %d): %s",
                    self._consecutive_failures,
                    exc,
                    extra={"story_id": self._story_id},
                )
                if self._consecutive_failures >= self._config.max_consecutive_failures:
                    self._pause()
                await asyncio.sleep(self._config.retry_interval_sec)
        logger.info("WebPoster stopped", extra={"story_id": self._story_id})

    async def stop(self) -> None:
        """グレースフル停止。_running = False にして session を閉じる。"""
        self._running = False
        if self._session is not None:
            await self._session.close()
            self._session = None
        logger.info("WebPoster stop requested", extra={"story_id": self._story_id})

    async def _send_batch(self) -> bool:
        """1バッチ取得 → POST → mark_posted。ログあり=True、なし=False。"""
        logs = await self._db.get_unposted_logs(
            self._story_id, limit=self._config.batch_size
        )
        if not logs:
            return False

        payload = self._build_payload(logs)
        assert self._session is not None

        async with self._session.post(
            self._config.receiver_url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status >= 400:
                raise aiohttp.ClientResponseError(
                    resp.request_info,
                    resp.history,
                    status=resp.status,
                    message=f"HTTP {resp.status}",
                )

        await self._db.mark_logs_posted(
            self._story_id, [log["id"] for log in logs]
        )
        logger.debug(
            "WebPoster sent %d logs",
            len(logs),
            extra={"story_id": self._story_id},
        )
        return True

    def _build_payload(self, logs: list[dict[str, Any]]) -> dict[str, Any]:
        """chat_logs レコードを receiver.php リクエスト形式に変換する。"""
        posts = []
        for log in logs:
            emotion = json.loads(log["emotion_snapshot"] or "{}")
            posts.append({
                "sim_datetime":     log["sim_datetime"],
                "turn_number":      log["turn_number"],
                "char_id":          log["char_id"],
                "msg_type":         log["msg_type"],
                "target_char_id":   log["target_char_id"],
                "place_id":         log["place_id"],
                "expression":       log["expression"],
                "message":          log["message"],
                "emotion_snapshot": emotion,
            })
        return {
            "auth_token": self._config.auth_token,
            "story_id":   self._story_id,
            "logs":       posts,
        }

    def _pause(self) -> None:
        """pause_duration_sec 秒の一時停止状態に遷移する。"""
        self._paused_until = time.monotonic() + self._config.pause_duration_sec
        logger.warning(
            "WebPoster paused for %ds due to consecutive failures",
            self._config.pause_duration_sec,
            extra={"story_id": self._story_id},
        )

    def _is_paused(self) -> bool:
        """一時停止中か確認。pause_duration_sec 経過で自動復帰。"""
        if self._paused_until is None:
            return False
        if time.monotonic() >= self._paused_until:
            self._paused_until = None
            logger.info("WebPoster resumed", extra={"story_id": self._story_id})
            return False
        return True

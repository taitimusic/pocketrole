from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from engine.config import (
    Config,
    ConfigValidationError,
    StoryWebPostTargetConfig,
    WebPosterConfig,
)
from engine.news_url import PublicHttpUrlValidationError, validate_public_http_url

if TYPE_CHECKING:
    from db.db_manager import DatabaseManager

logger = logging.getLogger(__name__)


def require_story_web_post_target(
    config: Config,
    story_id: str,
) -> StoryWebPostTargetConfig:
    target = config.web_post_targets.get(story_id)
    if target is None:
        raise ConfigValidationError(
            f"story '{story_id}' の Web 投稿設定がありません。"
            " config/web_post_targets.local.yaml を作成し、story ごとの"
            " receiver_url / auth_token を設定してください。"
        )
    if not target.enabled:
        return target
    if not target.receiver_url or not target.auth_token:
        raise ConfigValidationError(
            f"story '{story_id}' の Web 投稿設定が不完全です。"
            " receiver_url と auth_token の両方を設定してください。"
        )
    return target


def resolve_story_web_poster_config(
    config: Config,
    story_id: str,
) -> WebPosterConfig | None:
    target = require_story_web_post_target(config, story_id)
    if not target.enabled:
        return None
    try:
        receiver_url = validate_public_http_url(target.receiver_url)
    except PublicHttpUrlValidationError as exc:
        logger.warning("Invalid web post target URL for story=%s: %s", story_id, exc)
        return None
    return WebPosterConfig(
        receiver_url=receiver_url,
        batch_size=config.web_poster.batch_size,
        retry_interval_sec=config.web_poster.retry_interval_sec,
        max_consecutive_failures=config.web_poster.max_consecutive_failures,
        pause_duration_sec=config.web_poster.pause_duration_sec,
        auth_token=target.auth_token,
    )


def resolve_story_web_poster_configs(
    config: Config,
    story_ids: list[str],
) -> dict[str, WebPosterConfig]:
    resolved: dict[str, WebPosterConfig] = {}
    for story_id in story_ids:
        poster_config = resolve_story_web_poster_config(config, story_id)
        if poster_config is not None:
            resolved[story_id] = poster_config
    return resolved


async def resolve_story_web_poster_config_with_db(
    config: Config,
    story_id: str,
    db: "DatabaseManager",
) -> WebPosterConfig | None:
    """DB 優先で WebPoster 設定を解決する。

    解決順:
      1. stories テーブルの per-story web_receiver_url / web_auth_token
      2. system_settings の default_web_receiver_url / default_web_auth_token
      3. web_post_targets.local.yaml（YAML フォールバック・後方互換）

    URL と auth_token の両方が揃わない場合は WebPoster を起動しない（None）。
    """
    # 1. per-story DB 設定
    story = await db.get_story(story_id)
    url   = ((story or {}).get("web_receiver_url") or "").strip()
    token = ((story or {}).get("web_auth_token")   or "").strip()

    # 2. グローバルデフォルト
    if not url:
        url   = (await db.get_system_setting("default_web_receiver_url") or "").strip()
    if not token:
        token = (await db.get_system_setting("default_web_auth_token")   or "").strip()

    if url and token:
        try:
            url = validate_public_http_url(url)
        except PublicHttpUrlValidationError as exc:
            logger.warning("Invalid DB web post target URL for story=%s: %s", story_id, exc)
            return None
        return WebPosterConfig(
            receiver_url=url,
            auth_token=token,
            batch_size=config.web_poster.batch_size,
            retry_interval_sec=config.web_poster.retry_interval_sec,
            max_consecutive_failures=config.web_poster.max_consecutive_failures,
            pause_duration_sec=config.web_poster.pause_duration_sec,
        )

    # 3. YAML フォールバック（既存動作・後方互換）
    try:
        return resolve_story_web_poster_config(config, story_id)
    except ConfigValidationError:
        return None  # 未設定 → WebPoster なし

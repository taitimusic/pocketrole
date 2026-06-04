"""aiohttp admin service."""

from __future__ import annotations

from datetime import UTC, datetime
import io
import json
import os
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from aiohttp import web

from admin.archive_builder import ArchiveBuilder
from admin.llm_runtime_settings import (
    LLMRuntimeSettingsError,
    LLMRuntimeSettingsService,
)
from admin.story_onboarding import (
    CharacterEditingError,
    CharacterEditingService,
    ChapterDefinitionEditingError,
    ChapterDefinitionEditingService,
    DirectorPersonaEditingError,
    DirectorPersonaEditingService,
    EventAnomalyEditingError,
    EventAnomalyEditingService,
    PlaceEditingError,
    PlaceEditingService,
    StoryDefinitionEditingError,
    StoryDefinitionEditingService,
    StoryOnboardingError,
    StoryOnboardingService,
)
from admin.story_progress_reset import (
    RemoteResetSender,
    StoryProgressResetError,
    StoryProgressResetService,
)
from db.db_manager import DatabaseManager
from db.news_store import NewsArticleStore
from engine.config import NewsModeConfig, StoryWebPostTargetConfig
from engine.news_url import (
    FeedUrlValidationError,
    PublicHttpUrlValidationError,
    validate_feed_url,
    validate_public_http_url,
)

DB_KEY = web.AppKey("db", DatabaseManager)
RUNTIME_KEY = web.AppKey("runtime_controller", object)
ARCHIVE_ROOT_KEY = web.AppKey("archive_root", Path)
ARCHIVE_BUILDER_KEY = web.AppKey("archive_builder", ArchiveBuilder)
ADMIN_BASE_URL_KEY = web.AppKey("admin_base_url", str)
SESSION_COOKIE_SECURE_KEY = web.AppKey("session_cookie_secure", bool)
PUBLISHER_STATE_KEY = web.AppKey("publisher_state", dict)
STORY_ONBOARDING_KEY = web.AppKey("story_onboarding", StoryOnboardingService)
CHARACTER_EDITING_KEY = web.AppKey("character_editing", CharacterEditingService)
CHAPTER_DEFINITION_EDITING_KEY = web.AppKey(
    "chapter_definition_editing",
    ChapterDefinitionEditingService,
)
PLACE_EDITING_KEY = web.AppKey("place_editing", PlaceEditingService)
STORY_DEFINITION_EDITING_KEY = web.AppKey(
    "story_definition_editing",
    StoryDefinitionEditingService,
)
EVENT_ANOMALY_EDITING_KEY = web.AppKey(
    "event_anomaly_editing",
    EventAnomalyEditingService,
)
DIRECTOR_PERSONA_EDITING_KEY = web.AppKey(
    "director_persona_editing",
    DirectorPersonaEditingService,
)
LLM_RUNTIME_SETTINGS_KEY = web.AppKey(
    "llm_runtime_settings",
    LLMRuntimeSettingsService,
)
STORY_PROGRESS_RESET_KEY = web.AppKey(
    "story_progress_reset",
    StoryProgressResetService,
)
NEWS_STORE_KEY = web.AppKey("news_store", NewsArticleStore)
NEWS_MODE_CONFIG_KEY = web.AppKey("news_mode_config", NewsModeConfig)
STORY_MAPS_ROOT_KEY = web.AppKey("story_maps_root", Path)
MAX_BGM_UPLOAD_BYTES_KEY = web.AppKey("max_bgm_upload_bytes", int)
MAX_IMAGE_UPLOAD_BYTES_KEY = web.AppKey("max_image_upload_bytes", int)

BGM_PLACE_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
BGM_ALLOWED_EXTENSIONS = frozenset({".mp3", ".m4a", ".ogg"})
DEFAULT_MAX_BGM_UPLOAD_BYTES = 20 * 1024 * 1024
BGM_UPLOAD_CHUNK_SIZE = 256 * 1024
IMAGE_ALLOWED_CONTENT_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
IMAGE_ALLOWED_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp"})
DEFAULT_MAX_IMAGE_UPLOAD_BYTES = 8 * 1024 * 1024
IMAGE_UPLOAD_CHUNK_SIZE = 256 * 1024
NARRATOR_CHAR_ID = "_narrator"
NARRATOR_DISPLAY_NAME = "ナレーション"
NARRATOR_AVATAR_URL = "/assets/character_images/_system/narration/neutral.png"


def _json_ok(data: dict[str, Any], status: int = 200) -> web.Response:
    return web.json_response({"status": "ok", "data": data, "error": None}, status=status)


def _json_error(message: str, status: int) -> web.Response:
    return web.json_response({"status": "error", "data": None, "error": {"message": message}}, status=status)


async def _story_exists(db: DatabaseManager, story_id: str) -> bool:
    return await db.get_story(story_id) is not None


def _web_post_response(
    *,
    receiver_url: str,
    auth_token: str | None,
    story_id: str | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "receiver_url": receiver_url,
        "has_auth_token": bool(auth_token),
    }
    if story_id is not None:
        data["story_id"] = story_id
    return data


def _validate_receiver_url_for_request(url: str) -> str | web.Response:
    if not url:
        return ""
    try:
        return validate_public_http_url(url)
    except PublicHttpUrlValidationError as exc:
        return _json_error(f"invalid receiver url: {exc}", 400)


def _normalize_admin_locale(raw_locale: str | None) -> str:
    locale = str(raw_locale or "").strip().replace("_", "-")
    lower = locale.lower()
    if not lower:
        return ""
    if lower == "ja" or lower.startswith("ja-"):
        return "ja"
    if lower == "en" or lower.startswith("en-"):
        return "en"
    if lower in {"zh", "zh-tw", "zh-hant", "zh-hk", "zh-mo"} or lower.startswith("zh-hant-"):
        return "zh-TW"
    return ""


def _resolve_admin_locale(request: web.Request) -> str:
    query_locale = _normalize_admin_locale(request.query.get("lang"))
    if query_locale:
        return query_locale
    accept_language = request.headers.get("Accept-Language", "")
    for part in accept_language.split(","):
        locale = _normalize_admin_locale(part.split(";", 1)[0])
        if locale:
            return locale
    return "ja"


async def create_app(
    db: DatabaseManager,
    runtime_controller: Any,
    archive_root: str | Path,
    admin_base_url: str = "/admin",
    session_cookie_secure: bool = True,
    web_assets_root: str | Path | None = None,
    stories_root: str | Path | None = None,
    character_images_root: str | Path | None = None,
    story_maps_root: str | Path | None = None,
    story_metadata_root: str | Path | None = None,
    chatlog_root: str | Path | None = None,
    llm_runtime_path: str | Path | None = None,
    news_store_path: str | Path | None = None,
    news_mode_config: NewsModeConfig | None = None,
    story_web_post_targets: dict[str, StoryWebPostTargetConfig] | None = None,
    remote_reset_sender: RemoteResetSender | None = None,
    max_bgm_upload_bytes: int = DEFAULT_MAX_BGM_UPLOAD_BYTES,
    max_image_upload_bytes: int = DEFAULT_MAX_IMAGE_UPLOAD_BYTES,
) -> web.Application:
    package_root = Path(__file__).resolve().parent.parent
    resolved_web_assets_root = (
        Path(web_assets_root) if web_assets_root is not None else package_root / "web" / "assets"
    )
    app = web.Application(middlewares=[auth_middleware])
    app[DB_KEY] = db
    app[RUNTIME_KEY] = runtime_controller
    app[ARCHIVE_ROOT_KEY] = Path(archive_root)
    app[ARCHIVE_BUILDER_KEY] = ArchiveBuilder(db=db, output_root=archive_root, page_size_scenes=10)
    app[ADMIN_BASE_URL_KEY] = admin_base_url.rstrip("/")
    app[SESSION_COOKIE_SECURE_KEY] = session_cookie_secure
    app[PUBLISHER_STATE_KEY] = {"status": "idle", "last_run_at": None, "last_error": None}
    app[STORY_PROGRESS_RESET_KEY] = StoryProgressResetService(
        db=db,
        chatlog_root=chatlog_root or package_root / "web" / "chatlog",
        web_post_targets=story_web_post_targets,
        remote_reset_sender=remote_reset_sender,
    )
    app[LLM_RUNTIME_SETTINGS_KEY] = LLMRuntimeSettingsService(
        llm_runtime_path or package_root / "config" / "llm_runtime.local.yaml"
    )
    _news_store = NewsArticleStore(
        news_store_path or package_root / "db" / "news_articles.db"
    )
    await _news_store.initialize()
    app[NEWS_STORE_KEY] = _news_store
    app[NEWS_MODE_CONFIG_KEY] = news_mode_config or NewsModeConfig()
    app[STORY_MAPS_ROOT_KEY] = Path(
        story_maps_root if story_maps_root is not None else resolved_web_assets_root / "story_maps"
    )
    app[MAX_BGM_UPLOAD_BYTES_KEY] = max(1, int(max_bgm_upload_bytes))
    app[MAX_IMAGE_UPLOAD_BYTES_KEY] = max(1, int(max_image_upload_bytes))

    async def close_news_store(cleanup_app: web.Application) -> None:
        await cleanup_app[NEWS_STORE_KEY].close()

    app.on_cleanup.append(close_news_store)
    app[STORY_ONBOARDING_KEY] = StoryOnboardingService(
        db=db,
        stories_root=stories_root or package_root / "stories",
        character_images_root=(
            character_images_root or resolved_web_assets_root / "character_images"
        ),
        story_maps_root=story_maps_root or resolved_web_assets_root / "story_maps",
        story_metadata_root=story_metadata_root or resolved_web_assets_root / "story_metadata",
    )
    app[CHARACTER_EDITING_KEY] = CharacterEditingService(
        db=db,
        stories_root=stories_root or package_root / "stories",
        character_images_root=(
            character_images_root or resolved_web_assets_root / "character_images"
        ),
        story_maps_root=story_maps_root or resolved_web_assets_root / "story_maps",
        story_metadata_root=story_metadata_root or resolved_web_assets_root / "story_metadata",
    )
    app[CHAPTER_DEFINITION_EDITING_KEY] = ChapterDefinitionEditingService(
        db=db,
        stories_root=stories_root or package_root / "stories",
        story_metadata_root=story_metadata_root or resolved_web_assets_root / "story_metadata",
    )
    app[PLACE_EDITING_KEY] = PlaceEditingService(
        db=db,
        stories_root=stories_root or package_root / "stories",
        story_maps_root=story_maps_root or resolved_web_assets_root / "story_maps",
        story_metadata_root=story_metadata_root or resolved_web_assets_root / "story_metadata",
    )
    app[STORY_DEFINITION_EDITING_KEY] = StoryDefinitionEditingService(
        db=db,
        stories_root=stories_root or package_root / "stories",
        story_maps_root=story_maps_root or resolved_web_assets_root / "story_maps",
        story_metadata_root=story_metadata_root or resolved_web_assets_root / "story_metadata",
    )
    app[EVENT_ANOMALY_EDITING_KEY] = EventAnomalyEditingService(
        db=db,
        stories_root=stories_root or package_root / "stories",
        story_maps_root=story_maps_root or resolved_web_assets_root / "story_maps",
        story_metadata_root=story_metadata_root or resolved_web_assets_root / "story_metadata",
    )
    app[DIRECTOR_PERSONA_EDITING_KEY] = DirectorPersonaEditingService(
        db=db,
        stories_root=stories_root or package_root / "stories",
        story_metadata_root=story_metadata_root or resolved_web_assets_root / "story_metadata",
    )

    app.router.add_get("/admin/healthz", healthz)
    app.router.add_get("/admin", serve_shell)
    app.router.add_get("/admin/", serve_shell)
    app.router.add_get("/admin/stories", serve_shell)
    app.router.add_get(r"/admin/stories/{story_id:[a-z0-9_]+}", serve_shell)
    app.router.add_get(r"/admin/viewer/{story_id:[a-z0-9_]+}", serve_shell)
    app.router.add_get(r"/admin/onboarding/{story_id:[a-z0-9_]+}", serve_shell)
    app.router.add_get(r"/admin/characters/{story_id:[a-z0-9_]+}", serve_shell)
    app.router.add_get(r"/admin/places/{story_id:[a-z0-9_]+}", serve_shell)
    app.router.add_get(r"/admin/story-settings/{story_id:[a-z0-9_]+}", serve_shell)
    app.router.add_get(r"/admin/event-anomalies/{story_id:[a-z0-9_]+}", serve_shell)
    app.router.add_get(r"/admin/directors/{story_id:[a-z0-9_]+}", serve_shell)
    app.router.add_get(r"/admin/chapter-definitions/{story_id:[a-z0-9_]+}", serve_shell)
    app.router.add_get(r"/admin/chapters/{story_id:[a-z0-9_]+}", serve_shell)
    app.router.add_get(r"/admin/conversation-patterns/{story_id:[a-z0-9_]+}", serve_shell)
    app.router.add_get(r"/admin/scene-scripts/{story_id:[a-z0-9_]+}", serve_shell)
    app.router.add_get("/admin/settings", serve_shell)
    app.router.add_get("/admin/audit-logs", serve_shell)
    app.router.add_get(r"/admin/map-replay/{story_id:[a-z0-9_]+}", get_story_map_replay_page)
    app.router.add_static("/admin/assets", Path(__file__).parent / "assets")
    app.router.add_static("/assets", resolved_web_assets_root)

    app.router.add_post("/admin/api/v1/session/login", login)
    app.router.add_post("/admin/api/v1/session/logout", logout)
    app.router.add_get("/admin/api/v1/session", get_session)
    app.router.add_get("/admin/api/v1/dashboard", get_dashboard)
    app.router.add_get("/admin/api/v1/stories", list_stories)
    app.router.add_get(r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}", get_story)
    app.router.add_get(r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/live", get_story_live)
    app.router.add_get(r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/viewer", get_story_viewer)
    app.router.add_get(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/onboarding-template",
        get_story_onboarding_template,
    )
    app.router.add_post(r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/clone", post_story_clone)
    app.router.add_delete(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}",
        delete_story_handler,
    )
    app.router.add_get(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/characters/edit-template",
        get_story_character_edit_template,
    )
    app.router.add_put(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/characters",
        put_story_characters,
    )
    app.router.add_get(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/places/edit-template",
        get_story_place_edit_template,
    )
    app.router.add_put(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/places",
        put_story_places,
    )
    app.router.add_get(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/definition/edit-template",
        get_story_definition_edit_template,
    )
    app.router.add_put(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/definition",
        put_story_definition,
    )
    app.router.add_get(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/event-anomalies/edit-template",
        get_story_event_anomaly_edit_template,
    )
    app.router.add_put(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/event-anomalies",
        put_story_event_anomalies,
    )
    app.router.add_get(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/director-personas/edit-template",
        get_story_director_persona_edit_template,
    )
    app.router.add_put(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/director-personas",
        put_story_director_personas,
    )
    app.router.add_get(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/chapter-definitions/edit-template",
        get_story_chapter_definition_edit_template,
    )
    app.router.add_put(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/chapter-definitions",
        put_story_chapter_definitions,
    )
    app.router.add_get(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/conversation-patterns",
        get_conversation_patterns,
    )
    app.router.add_put(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/conversation-patterns",
        put_conversation_patterns,
    )
    app.router.add_get(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/scene-scripts",
        get_scene_scripts,
    )
    app.router.add_delete(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/scene-scripts/from-turn/{turn:\d+}",
        delete_scene_scripts_from_turn_handler,
    )
    app.router.add_get(r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/archive", get_story_archive)
    app.router.add_get(r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/archive/pages/{page:\d+}", get_story_archive_page)
    app.router.add_put(r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/runtime", put_story_runtime)
    app.router.add_post(r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/runtime/restart", restart_story_runtime)
    app.router.add_post(r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/runtime/reset", reset_story_runtime)
    app.router.add_put(r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/publication", put_story_publication)
    app.router.add_post(r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/archive-builds", post_story_archive_build)
    app.router.add_get(r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/downloads/story.zip", download_story)
    app.router.add_get("/admin/api/v1/settings", get_settings)
    app.router.add_put("/admin/api/v1/settings/archive-publish", put_archive_settings)
    app.router.add_put("/admin/api/v1/settings/llm-runtime", put_llm_runtime_settings)
    app.router.add_get("/admin/api/v1/settings/web-post", get_settings_web_post)
    app.router.add_put("/admin/api/v1/settings/web-post", put_settings_web_post)
    app.router.add_get(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/web-post",
        get_story_web_post,
    )
    app.router.add_put(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/web-post",
        put_story_web_post,
    )
    app.router.add_get("/admin/api/v1/audit-logs", get_audit_logs)
    app.router.add_get("/admin/api/v1/replay", get_story_replay_api)
    app.router.add_get(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/bgm-config",
        get_story_bgm_config,
    )
    app.router.add_put(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/bgm-config",
        put_story_bgm_config,
    )

    # Chapter Proposals（Phase 4）
    app.router.add_get(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/chapters",
        get_story_chapters,
    )
    app.router.add_get(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/chapter-proposals",
        get_chapter_proposals,
    )
    app.router.add_post(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/chapter-proposals/generate",
        post_generate_chapter_proposal,
    )
    app.router.add_put(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/chapter-proposals/{proposal_id:\d+}/approve",
        put_approve_chapter_proposal,
    )
    app.router.add_put(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/chapter-proposals/{proposal_id:\d+}/reject",
        put_reject_chapter_proposal,
    )
    app.router.add_put(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/director-personas/{persona_id:[a-z0-9_]+}/activate",
        put_activate_director_persona,
    )
    app.router.add_post(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/chapters/{chapter_db_id:\d+}/activate",
        post_activate_chapter,
    )
    app.router.add_post(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/chapters/{chapter_db_id:\d+}/close",
        post_close_chapter,
    )
    app.router.add_post(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/chapters/{chapter_db_id:\d+}/reopen",
        post_reopen_chapter,
    )
    app.router.add_put(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/chapters/{chapter_db_id:\d+}/current-beat",
        put_chapter_current_beat,
    )

    # ── 時事モード: RSS フィード管理 / 記事 / per-story 設定 ──────────────
    app.router.add_get("/admin/news-mode", serve_shell)
    app.router.add_get("/admin/api/v1/news/feeds", get_news_feeds)
    app.router.add_post("/admin/api/v1/news/feeds", post_news_feed)
    app.router.add_put(r"/admin/api/v1/news/feeds/{feed_id:\d+}", put_news_feed)
    app.router.add_delete(r"/admin/api/v1/news/feeds/{feed_id:\d+}", delete_news_feed)
    app.router.add_post("/admin/api/v1/news/fetch-now", post_news_fetch_now)
    app.router.add_get("/admin/api/v1/news/articles", get_news_articles)
    app.router.add_post("/admin/api/v1/news/reset", post_news_reset_articles)
    app.router.add_post("/admin/api/v1/news/reset-all", post_news_reset_all)
    app.router.add_get("/admin/api/v1/news/tags", get_news_tags)
    app.router.add_get(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/news-mode",
        get_story_news_mode,
    )
    app.router.add_put(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/news-mode",
        put_story_news_mode,
    )
    app.router.add_get(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/news-tag-filter",
        get_story_news_tag_filter,
    )
    app.router.add_put(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/news-tag-filter",
        put_story_news_tag_filter,
    )
    app.router.add_get(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/utterance-settings",
        get_story_utterance_settings,
    )
    app.router.add_put(
        r"/admin/api/v1/stories/{story_id:[a-z0-9_]+}/utterance-settings",
        put_story_utterance_settings,
    )

    return app


@web.middleware
async def auth_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    if not request.path.startswith("/admin/api/v1/"):
        return await handler(request)
    if request.path == "/admin/api/v1/session/login":
        return await handler(request)

    actor = await authenticate_request(request)
    if actor is None:
        return _json_error("Unauthorized", 401)
    request["actor"] = actor
    return await handler(request)


async def authenticate_request(request: web.Request) -> dict[str, str] | None:
    db = request.app[DB_KEY]
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.removeprefix("Bearer ").strip()
        token_row = await db.authenticate_admin_api_token(token)
        if token_row is not None:
            return {"actor_type": "token", "actor_label": token_row["label"]}

    session_token = request.cookies.get("admin_session")
    if session_token:
        session_row = await db.get_admin_session(session_token)
        if session_row is not None:
            return {"actor_type": "user", "actor_label": session_row["username"]}
    return None


async def serve_shell(request: web.Request) -> web.Response:
    base_url = request.app[ADMIN_BASE_URL_KEY]
    locale = _resolve_admin_locale(request)
    html = f"""<!DOCTYPE html>
<html lang="{locale}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PocketRole Admin</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">
  <link rel="stylesheet" href="{base_url}/assets/tokens.css">
  <link rel="stylesheet" href="{base_url}/assets/layout.css">
  <link rel="stylesheet" href="{base_url}/assets/components.css">
  <link rel="stylesheet" href="{base_url}/assets/admin_style.css">
  <script src="{base_url}/assets/ui/theme.js"></script>
</head>
<body>
  <div id="admin-app"></div>
  <div id="toast-stack" aria-live="polite" aria-atomic="false"></div>
  <script>
    window.POCKETROLE_ADMIN = {{
      apiBaseUrl: "{base_url}/api/v1",
      locale: "{locale}"
    }};
  </script>
  <script src="{base_url}/assets/ui/icons.js"></script>
  <script src="{base_url}/assets/ui/toast.js"></script>
  <script src="{base_url}/assets/ui/skeleton.js"></script>
  <script src="{base_url}/assets/i18n.js"></script>
  <script src="{base_url}/assets/admin_condition_builder.js"></script>
  <script src="{base_url}/assets/admin_chapter_beats.js"></script>
  <script src="{base_url}/assets/admin_app.js"></script>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")


async def healthz(request: web.Request) -> web.Response:
    return _json_ok(
        {
            "service": "pocketrole-admin",
            "db": {"status": "ok", "backend": request.app[DB_KEY].backend_kind},
            "runtime": {"states": request.app[RUNTIME_KEY].list_states()},
            "publisher": dict(request.app[PUBLISHER_STATE_KEY]),
        }
    )


async def login(request: web.Request) -> web.Response:
    payload = await request.json()
    username = str(payload.get("username", ""))
    password = str(payload.get("password", ""))
    db = request.app[DB_KEY]
    user = await db.verify_admin_credentials(username, password)
    if user is None:
        return _json_error("Invalid credentials", 401)
    session_token = await db.create_admin_session(user["id"])
    response = _json_ok({"username": username})
    response.set_cookie(
        "admin_session",
        session_token,
        httponly=True,
        secure=request.app[SESSION_COOKIE_SECURE_KEY],
        samesite="Lax",
    )
    return response


async def logout(request: web.Request) -> web.Response:
    db = request.app[DB_KEY]
    session_token = request.cookies.get("admin_session")
    if session_token:
        await db.delete_admin_session(session_token)
    response = _json_ok({"logged_out": True})
    response.del_cookie("admin_session")
    return response


async def get_session(request: web.Request) -> web.Response:
    return _json_ok({"actor": request["actor"]})


async def get_dashboard(request: web.Request) -> web.Response:
    db = request.app[DB_KEY]
    stories = await db.get_stories()
    publications = await db.list_story_publications()
    runtime_states = request.app[RUNTIME_KEY].list_states()
    publications_by_id = {row["story_id"]: row for row in publications}
    latest_chat_by_story = await db.get_latest_chat_per_story()
    daily_turns = await db.count_chat_logs_by_day(days=7)
    recent_chats = await db.get_recent_dashboard_chats(limit=6)
    audit_logs = await db.list_admin_audit_logs(limit=5)

    today_turn_count = daily_turns[-1]["count"] if daily_turns else 0
    public_count = sum(1 for row in publications if row["visibility"] == "public")
    active_runtime_count = sum(1 for state in runtime_states.values() if state == "running")

    runtime_summary = []
    for story in stories:
        story_id = story["id"]
        latest = latest_chat_by_story.get(story_id) or {}
        publication = publications_by_id.get(story_id) or {}
        runtime_summary.append(
            {
                "story_id": story_id,
                "title": story["title"],
                "state": runtime_states.get(story_id, "stopped"),
                "llm_provider": story.get("llm_provider"),
                "llm_model": story.get("llm_model"),
                "visibility": publication.get("visibility", "draft"),
                "last_chat_at": latest.get("created_at"),
                "last_sim_datetime": latest.get("sim_datetime"),
            }
        )

    return _json_ok(
        {
            "story_count": len(stories),
            "public_story_count": public_count,
            "runtime_states": runtime_states,
            "kpis": {
                "story_count": len(stories),
                "public_count": public_count,
                "today_turn_count": today_turn_count,
                "active_runtime_count": active_runtime_count,
            },
            "daily_turn_counts": daily_turns,
            "runtime_summary": runtime_summary,
            "recent_chats": recent_chats,
            "recent_audit_logs": audit_logs,
        }
    )


async def list_stories(request: web.Request) -> web.Response:
    db = request.app[DB_KEY]
    stories = await db.get_stories()
    runtime_states = request.app[RUNTIME_KEY].list_states()
    publications = {row["story_id"]: row for row in await db.list_story_publications()}
    payload = []
    for story in stories:
        publication = publications.get(story["id"])
        payload.append(
            {
                "story_id": story["id"],
                "title": story["title"],
                "runtime_state": runtime_states.get(story["id"], "stopped"),
                "visibility": publication["visibility"] if publication else "draft",
                "published_at": publication["published_at"] if publication else None,
            }
        )
    return _json_ok({"stories": payload})


async def get_story(request: web.Request) -> web.Response:
    db = request.app[DB_KEY]
    story_id = request.match_info["story_id"]
    story = await db.get_story(story_id)
    if story is None:
        return _json_error("Story not found", 404)
    publication = await db.get_story_publication(story_id)
    return _json_ok(
        {
            "story": story,
            "publication": publication or {"visibility": "draft"},
            "runtime_state": request.app[RUNTIME_KEY].list_states().get(story_id, "stopped"),
        }
    )


async def get_story_live(request: web.Request) -> web.Response:
    db = request.app[DB_KEY]
    story_id = request.match_info["story_id"]
    logs = await db.get_recent_chat_logs(story_id, limit=20)
    return _json_ok({"logs": logs})


async def get_story_viewer(request: web.Request) -> web.Response:
    db = request.app[DB_KEY]
    story_id = request.match_info["story_id"]
    snapshot = await db.get_story_viewer_snapshot(story_id, limit=50)
    if snapshot is None:
        return _json_error("Story not found", 404)

    publication = await db.get_story_publication(story_id)
    for character in snapshot["characters"]:
        character["image_base_url"] = f"/assets/character_images/{story_id}/{character['char_id']}"

    snapshot["runtime"] = {
        "state": request.app[RUNTIME_KEY].list_states().get(story_id, "stopped"),
    }
    snapshot["publication"] = publication or {"visibility": "draft", "published_at": None}
    return _json_ok(snapshot)


async def get_story_onboarding_template(request: web.Request) -> web.Response:
    service = request.app[STORY_ONBOARDING_KEY]
    story_id = request.match_info["story_id"]
    payload = await service.get_template_payload(story_id)
    if payload is None:
        return _json_error("Story template not found", 404)
    return _json_ok(payload)


async def post_story_clone(request: web.Request) -> web.Response:
    service = request.app[STORY_ONBOARDING_KEY]
    template_story_id = request.match_info["story_id"]
    uploaded_images: dict[str, bytes] = {}
    try:
        if request.content_type.startswith("multipart/"):
            reader = await request.multipart()
            payload: dict[str, Any] | None = None
            async for field in reader:
                if field.name == "payload":
                    payload = json.loads(await field.text())
                    continue
                if field.name and field.name.startswith("character_image__"):
                    char_id = field.name.removeprefix("character_image__")
                    uploaded_images[char_id] = await _read_image_upload(
                        field, request.app[MAX_IMAGE_UPLOAD_BYTES_KEY]
                    )
            if payload is None:
                return _json_error("payload is required", 400)
        else:
            payload = await request.json()
    except json.JSONDecodeError as exc:
        return _json_error(str(exc) or "invalid clone payload", 400)
    except ImageUploadTooLarge as exc:
        return _json_error(str(exc), 413)
    except ImageUploadError as exc:
        return _json_error(str(exc), 400)

    try:
        result = await service.clone_story(template_story_id, payload, uploaded_images=uploaded_images)
    except StoryOnboardingError as exc:
        return _json_error(str(exc), 400)

    runtime_controller = request.app[RUNTIME_KEY]
    register_story = getattr(runtime_controller, "register_story", None)
    if callable(register_story):
        register_story(result["story_id"])
    await audit(
        request,
        "story.clone.create",
        story_id=result["story_id"],
        result="ok",
        payload_summary=result["title"],
    )
    return _json_ok(result, status=201)


_STORY_FS_DIRS: list[str] = [
    "stories/{story_id}",
    "web/assets/character_images/{story_id}",
    "web/assets/story_maps/{story_id}",
    "story_maps/{story_id}",
    "web/chatlog/{story_id}",
]
_STORY_FS_FILES: list[str] = [
    "web/assets/story_metadata/{story_id}.json",
]

# app.py は pocketrole/admin/app.py のため、pocketrole/ が parents[1]
_POCKETROLE_ROOT = Path(__file__).resolve().parent.parent


def _safe_story_path(story_id: str, rel_template: str) -> Path | None:
    """パストラバーサル防止: 解決済みパスが _POCKETROLE_ROOT 配下かを検証。"""
    resolved = (_POCKETROLE_ROOT / rel_template.format(story_id=story_id)).resolve()
    try:
        resolved.relative_to(_POCKETROLE_ROOT.resolve())
        return resolved
    except ValueError:
        return None


async def delete_story_handler(request: web.Request) -> web.Response:
    """ストーリーとその関連データを完全削除する。

    payload:
        confirm_story_id (str): story_id と完全一致することが必須（誤操作防止）
        delete_files (bool):    True のとき YAML・画像等 FS リソースも削除
    """
    db = request.app[DB_KEY]
    story_id = request.match_info["story_id"]
    payload = await request.json() if request.content_length else {}

    # 確認トークンチェック
    if payload.get("confirm_story_id") != story_id:
        return _json_error(
            "confirm_story_id が一致しません。story_id を正確に入力してください。", 400
        )

    story = await db.get_story(story_id)
    if story is None:
        return _json_error("story not found", 404)
    if story.get("is_active"):
        return _json_error(f"Story '{story_id}' is currently active. Stop the engine first.", 409)

    try:
        remote_public_logs_reset = await request.app[
            STORY_PROGRESS_RESET_KEY
        ].reset_remote_public_logs(story_id)
    except StoryProgressResetError as exc:
        return _json_error(str(exc), 500)

    # DB 削除（CASCADE 削除）
    try:
        deleted = await db.delete_story_completely(story_id)
    except ValueError as exc:
        return _json_error(str(exc), 409)

    if not deleted:
        return _json_error("story not found", 404)

    # FS 削除（オプション）
    deleted_paths: list[str] = []
    if payload.get("delete_files", False):
        import shutil
        for tmpl in _STORY_FS_DIRS:
            p = _safe_story_path(story_id, tmpl)
            if p and p.exists() and p.is_dir():
                shutil.rmtree(str(p), ignore_errors=True)
                deleted_paths.append(tmpl.format(story_id=story_id))
        for tmpl in _STORY_FS_FILES:
            p = _safe_story_path(story_id, tmpl)
            if p and p.exists() and p.is_file():
                p.unlink(missing_ok=True)
                deleted_paths.append(tmpl.format(story_id=story_id))

    await audit(
        request, "story.delete", story_id=None,
        result="ok",
        payload_summary=f"story_id={story_id} delete_files={payload.get('delete_files', False)}"
                        f" paths={deleted_paths}",
    )
    return _json_ok({
        "story_id": story_id,
        "deleted": True,
        "deleted_files": deleted_paths,
        "remote_public_logs_reset": remote_public_logs_reset,
    })


async def get_story_character_edit_template(request: web.Request) -> web.Response:
    service = request.app[CHARACTER_EDITING_KEY]
    story_id = request.match_info["story_id"]
    payload = await service.get_edit_payload(story_id)
    if payload is None:
        return _json_error("Story not found", 404)

    runtime_state = request.app[RUNTIME_KEY].list_states().get(story_id, "stopped")
    payload["runtime_state"] = runtime_state
    if runtime_state != "stopped":
        payload["editable"] = False
        payload["read_only_reason"] = "story_running"
    if not payload.get("imported"):
        payload["editable"] = False
        payload["read_only_reason"] = payload.get("read_only_reason") or "story_not_imported"
    return _json_ok(payload)


async def put_story_characters(request: web.Request) -> web.Response:
    story_id = request.match_info["story_id"]
    runtime_state = request.app[RUNTIME_KEY].list_states().get(story_id, "stopped")
    if runtime_state != "stopped":
        return _json_error("story must be stopped before editing characters", 409)

    try:
        payload, uploaded_images = await _read_payload_with_uploaded_character_images(request)
    except ImageUploadTooLarge as exc:
        return _json_error(str(exc), 413)
    except ImageUploadError as exc:
        return _json_error(str(exc), 400)
    except (json.JSONDecodeError, web.HTTPBadRequest) as exc:
        return _json_error(str(exc) or "invalid character edit payload", 400)
    service = request.app[CHARACTER_EDITING_KEY]
    try:
        result = await service.update_characters(
            story_id,
            payload,
            uploaded_images=uploaded_images,
        )
    except CharacterEditingError as exc:
        return _json_error(str(exc), 400)

    await audit(
        request,
        "story.characters.update",
        story_id=story_id,
        result="ok",
        payload_summary=(
            f"{result['character_count']} characters"
            f" (+{result.get('characters_added', 0)}/-{result.get('characters_removed', 0)}"
            f"/rename:{result.get('characters_renamed', 0)})"
        ),
    )
    return _json_ok(result)


async def get_story_place_edit_template(request: web.Request) -> web.Response:
    service = request.app[PLACE_EDITING_KEY]
    story_id = request.match_info["story_id"]
    payload = await service.get_edit_payload(story_id)
    if payload is None:
        return _json_error("Story not found", 404)

    runtime_state = request.app[RUNTIME_KEY].list_states().get(story_id, "stopped")
    payload["runtime_state"] = runtime_state
    if runtime_state != "stopped":
        payload["editable"] = False
        payload["read_only_reason"] = "story_running"
    if not payload.get("imported"):
        payload["editable"] = False
        payload["read_only_reason"] = payload.get("read_only_reason") or "story_not_imported"
    return _json_ok(payload)


async def put_story_places(request: web.Request) -> web.Response:
    story_id = request.match_info["story_id"]
    runtime_state = request.app[RUNTIME_KEY].list_states().get(story_id, "stopped")
    if runtime_state != "stopped":
        return _json_error("story must be stopped before editing places", 409)

    try:
        payload, uploaded_images = await _read_payload_with_uploaded_place_images(request)
    except ImageUploadTooLarge as exc:
        return _json_error(str(exc), 413)
    except ImageUploadError as exc:
        return _json_error(str(exc), 400)
    except (json.JSONDecodeError, web.HTTPBadRequest) as exc:
        return _json_error(str(exc) or "invalid place edit payload", 400)

    service = request.app[PLACE_EDITING_KEY]
    try:
        result = await service.update_places(story_id, payload, uploaded_images=uploaded_images)
    except PlaceEditingError as exc:
        return _json_error(str(exc), 400)

    await audit(
        request,
        "story.places.update",
        story_id=story_id,
        result="ok",
        payload_summary=(
            f"{result['place_count']} places"
            f" (+{result['places_added']}/-{result['places_removed']}"
            f"/rename:{result.get('places_renamed', 0)})"
        ),
    )
    return _json_ok(result)


async def get_story_definition_edit_template(request: web.Request) -> web.Response:
    service = request.app[STORY_DEFINITION_EDITING_KEY]
    story_id = request.match_info["story_id"]
    payload = await service.get_edit_payload(story_id)
    if payload is None:
        return _json_error("Story not found", 404)

    runtime_state = request.app[RUNTIME_KEY].list_states().get(story_id, "stopped")
    payload["runtime_state"] = runtime_state
    if runtime_state != "stopped":
        payload["editable"] = False
        payload["read_only_reason"] = "story_running"
    if not payload.get("imported"):
        payload["editable"] = False
        payload["read_only_reason"] = payload.get("read_only_reason") or "story_not_imported"
    return _json_ok(payload)


async def put_story_definition(request: web.Request) -> web.Response:
    story_id = request.match_info["story_id"]
    runtime_state = request.app[RUNTIME_KEY].list_states().get(story_id, "stopped")
    if runtime_state != "stopped":
        return _json_error("story must be stopped before editing story settings", 409)

    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        return _json_error(str(exc) or "invalid story definition payload", 400)

    service = request.app[STORY_DEFINITION_EDITING_KEY]
    try:
        result = await service.update_definition(story_id, payload)
    except StoryDefinitionEditingError as exc:
        return _json_error(str(exc), 400)

    await audit(
        request,
        "story.definition.update",
        story_id=story_id,
        result="ok",
        payload_summary="story definition",
    )
    return _json_ok(result)


async def get_story_event_anomaly_edit_template(request: web.Request) -> web.Response:
    service = request.app[EVENT_ANOMALY_EDITING_KEY]
    story_id = request.match_info["story_id"]
    payload = await service.get_edit_payload(story_id)
    if payload is None:
        return _json_error("Story not found", 404)

    runtime_state = request.app[RUNTIME_KEY].list_states().get(story_id, "stopped")
    payload["runtime_state"] = runtime_state
    if runtime_state != "stopped":
        payload["editable"] = False
        payload["read_only_reason"] = "story_running"
    if not payload.get("imported"):
        payload["editable"] = False
        payload["read_only_reason"] = payload.get("read_only_reason") or "story_not_imported"
    return _json_ok(payload)


async def put_story_event_anomalies(request: web.Request) -> web.Response:
    story_id = request.match_info["story_id"]
    runtime_state = request.app[RUNTIME_KEY].list_states().get(story_id, "stopped")
    if runtime_state != "stopped":
        return _json_error("story must be stopped before editing events and anomalies", 409)

    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        return _json_error(str(exc) or "invalid event/anomaly edit payload", 400)

    service = request.app[EVENT_ANOMALY_EDITING_KEY]
    try:
        result = await service.update_event_anomalies(story_id, payload)
    except EventAnomalyEditingError as exc:
        return _json_error(str(exc), 400)

    await audit(
        request,
        "story.event_anomalies.update",
        story_id=story_id,
        result="ok",
        payload_summary=(
            f"{result['event_count']} events (+{result['events_added']}/-{result['events_removed']})"
            f" / {result['anomaly_count']} anomalies"
            f" (+{result['anomalies_added']}/-{result['anomalies_removed']})"
        ),
    )
    return _json_ok(result)


async def get_story_director_persona_edit_template(request: web.Request) -> web.Response:
    service = request.app[DIRECTOR_PERSONA_EDITING_KEY]
    db = request.app[DB_KEY]
    story_id = request.match_info["story_id"]
    payload = await service.get_edit_payload(story_id)
    if payload is None:
        return _json_error("Story not found", 404)

    runtime_state = request.app[RUNTIME_KEY].list_states().get(story_id, "stopped")
    payload["runtime_state"] = runtime_state
    director_personas = await db.get_all_director_personas(story_id)
    if not director_personas:
        default_active = str(payload.get("default_active") or "").strip()
        director_personas = [
            {
                "persona_id": str(persona.get("persona_id") or "").strip(),
                "name": str(persona.get("name") or "").strip(),
                "aesthetic_json": dict(persona.get("aesthetic") or {}),
                "values_json": list(persona.get("values") or []),
                "traits_json": list(persona.get("traits") or []),
                "is_active": 1
                if str(persona.get("persona_id") or "").strip() == default_active
                else 0,
            }
            for persona in list(payload.get("personas") or [])
            if str(persona.get("persona_id") or "").strip()
        ]
    active_director = await db.get_active_director_persona(story_id)
    if active_director is None:
        active_director = next(
            (
                persona
                for persona in director_personas
                if int(persona.get("is_active") or 0) == 1
            ),
            None,
        )
    payload["active_director_persona"] = active_director
    payload["director_personas"] = director_personas
    payload["director_swap_history"] = await db.get_director_swap_history(story_id)
    if runtime_state != "stopped":
        payload["editable"] = False
        payload["read_only_reason"] = "story_running"
    if not payload.get("imported"):
        payload["editable"] = False
        payload["read_only_reason"] = payload.get("read_only_reason") or "story_not_imported"
    return _json_ok(payload)


async def put_story_director_personas(request: web.Request) -> web.Response:
    story_id = request.match_info["story_id"]
    runtime_state = request.app[RUNTIME_KEY].list_states().get(story_id, "stopped")
    if runtime_state != "stopped":
        return _json_error("story must be stopped before editing director personas", 409)

    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        return _json_error(str(exc) or "invalid director persona edit payload", 400)

    service = request.app[DIRECTOR_PERSONA_EDITING_KEY]
    try:
        result = await service.update_director_personas(story_id, payload)
    except DirectorPersonaEditingError as exc:
        return _json_error(str(exc), 400)

    await audit(
        request,
        "story.director_personas.update",
        story_id=story_id,
        result="ok",
        payload_summary=(
            f"{result['persona_count']} personas"
            f" (+{result['personas_added']}/-{result['personas_removed']})"
        ),
    )
    return _json_ok(result)


async def get_story_chapter_definition_edit_template(request: web.Request) -> web.Response:
    service = request.app[CHAPTER_DEFINITION_EDITING_KEY]
    story_id = request.match_info["story_id"]
    payload = await service.get_edit_payload(story_id)
    if payload is None:
        return _json_error("Story not found", 404)

    runtime_state = request.app[RUNTIME_KEY].list_states().get(story_id, "stopped")
    payload["runtime_state"] = runtime_state
    if runtime_state != "stopped":
        payload["editable"] = False
        payload["read_only_reason"] = "story_running"
    if not payload.get("imported"):
        payload["editable"] = False
        payload["read_only_reason"] = payload.get("read_only_reason") or "story_not_imported"
    return _json_ok(payload)


async def put_story_chapter_definitions(request: web.Request) -> web.Response:
    story_id = request.match_info["story_id"]
    runtime_state = request.app[RUNTIME_KEY].list_states().get(story_id, "stopped")
    if runtime_state != "stopped":
        return _json_error("story must be stopped before editing chapters", 409)

    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        return _json_error(str(exc) or "invalid chapter edit payload", 400)

    service = request.app[CHAPTER_DEFINITION_EDITING_KEY]
    try:
        result = await service.update_chapters(story_id, payload)
    except ChapterDefinitionEditingError as exc:
        return _json_error(str(exc), 400)

    await audit(
        request,
        "story.chapter_definitions.update",
        story_id=story_id,
        result="ok",
        payload_summary=(
            f"{result['chapter_count']} chapters"
            f" (+{result['chapters_added']}/-{result['chapters_removed']})"
        ),
    )
    return _json_ok(result)


class ImageUploadError(ValueError):
    """Raised when an uploaded image request is invalid."""


class ImageUploadTooLarge(ValueError):
    """Raised when an uploaded image exceeds the configured byte limit."""


def _validate_image_upload_metadata(field: Any) -> None:
    extension = Path(field.filename or "").suffix.lower()
    if extension not in IMAGE_ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(IMAGE_ALLOWED_EXTENSIONS))
        raise ImageUploadError(f"image file extension must be one of: {allowed}")

    content_type = str(field.headers.get("Content-Type", "")).split(";", 1)[0].lower()
    if content_type not in IMAGE_ALLOWED_CONTENT_TYPES:
        allowed = ", ".join(sorted(IMAGE_ALLOWED_CONTENT_TYPES))
        raise ImageUploadError(f"image content type must be one of: {allowed}")


async def _read_image_upload(field: Any, max_bytes: int) -> bytes:
    _validate_image_upload_metadata(field)
    chunks: list[bytes] = []
    total_bytes = 0
    while True:
        chunk = await field.read_chunk(size=IMAGE_UPLOAD_CHUNK_SIZE)
        if not chunk:
            break
        total_bytes += len(chunk)
        if total_bytes > max_bytes:
            raise ImageUploadTooLarge(f"image upload exceeds {max_bytes} bytes")
        chunks.append(chunk)
    if total_bytes <= 0:
        raise ImageUploadError("image file is empty")
    return b"".join(chunks)


async def _read_payload_with_uploaded_character_images(
    request: web.Request,
) -> tuple[dict[str, Any], dict[str, dict[str, bytes]]]:
    uploaded_images: dict[str, dict[str, bytes]] = {}
    if request.content_type.startswith("multipart/"):
        reader = await request.multipart()
        payload: dict[str, Any] | None = None
        async for field in reader:
            if field.name == "payload":
                payload = json.loads(await field.text())
                continue
            if field.name and field.name.startswith("character_image__"):
                image_key = field.name.removeprefix("character_image__")
                if "__" in image_key:
                    char_id, expression = image_key.split("__", 1)
                else:
                    char_id, expression = image_key, "neutral"
                uploaded_images.setdefault(char_id, {})[expression] = await _read_image_upload(
                    field, request.app[MAX_IMAGE_UPLOAD_BYTES_KEY]
                )
        if payload is None:
            raise web.HTTPBadRequest(text="payload is required")
        return payload, uploaded_images
    return await request.json(), uploaded_images


async def _read_payload_with_uploaded_place_images(
    request: web.Request,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    uploaded_images: dict[str, bytes] = {}
    if request.content_type.startswith("multipart/"):
        reader = await request.multipart()
        payload: dict[str, Any] | None = None
        async for field in reader:
            if field.name == "payload":
                payload = json.loads(await field.text())
                continue
            if field.name and field.name.startswith("place_image__"):
                place_id = field.name.removeprefix("place_image__")
                uploaded_images[place_id] = await _read_image_upload(
                    field, request.app[MAX_IMAGE_UPLOAD_BYTES_KEY]
                )
        if payload is None:
            raise web.HTTPBadRequest(text="payload is required")
        return payload, uploaded_images
    return await request.json(), uploaded_images


async def get_story_map_replay_page(request: web.Request) -> web.Response:
    story_id = request.match_info["story_id"]
    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{story_id} — Map Replay</title>
  <link rel="stylesheet" href="/assets/map_replay.css">
</head>
<body>
  <div class="replay-controls">
    <button id="play-toggle" type="button">再生</button>
    <button id="next-event" type="button">1件送り</button>
    <button id="reset-replay" type="button">先頭へ戻る</button>
    <button id="bgm-toggle" type="button">BGM ON</button>
    <label for="speed-select">速度</label>
    <select id="speed-select">
      <option value="0.5">0.5x</option>
      <option value="1" selected>1x</option>
      <option value="2">2x</option>
      <option value="4">4x</option>
    </select>
    <label for="bgm-volume">音量</label>
    <input id="bgm-volume" class="bgm-volume" type="range" min="0" max="1" step="0.05" value="0.55">
  </div>
  <div class="stage-shell">
    <div id="map-stage" aria-label="マップ再生ステージ"></div>
    <div id="unsupported-state" class="unsupported-state" hidden></div>
    <div class="minimal-hud">
      <span id="status-speaker">話者: -</span>
      <span id="status-place">場所: -</span>
      <span id="status-progress">0 / 0</span>
    </div>
  </div>
  <script>
    window.POCKETROLE_MAP_REPLAY = {{
      storyId: {json.dumps(story_id)},
      apiUrl: "/admin/api/v1/replay",
      replayPageUrl: "/admin/map-replay/{story_id}",
      viewerPageUrl: "/admin/viewer/{story_id}",
      imageBaseUrl: "/assets/character_images",
      storyMapBaseUrl: "/assets/story_maps",
      defaultBgmUrl: "/assets/audio/pocketrole_bgm.mp3"
    }};
  </script>
  <script src="/assets/phaser.min.js"></script>
  <script src="/assets/map_replay.js"></script>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")


async def get_story_replay_api(request: web.Request) -> web.Response:
    db = request.app[DB_KEY]
    story_id = request.rel_url.query.get("story_id", "")
    if not story_id:
        return web.json_response({"status": "error", "message": "story_id required"}, status=400)

    snapshot = await db.get_story_viewer_snapshot(story_id, limit=300)
    if snapshot is None:
        return web.json_response({"status": "error", "message": "story not found"}, status=404)

    def replay_char_name(char_id: str, fallback: str | None = None) -> str:
        if char_id == NARRATOR_CHAR_ID:
            return NARRATOR_DISPLAY_NAME
        return fallback or char_id

    events = []
    includes_narrator = False
    for i, log in enumerate(snapshot["logs"]):
        char_id = str(log.get("char_id") or "")
        if char_id == NARRATOR_CHAR_ID:
            includes_narrator = True
        events.append(
            {
                "seq": i + 1,
                "sim_datetime": log.get("sim_datetime"),
                "char_id": char_id,
                "char_name": replay_char_name(char_id, log.get("speaker_name")),
                "place_id": log.get("place_id"),
                "message": log.get("message"),
                "expression": log.get("expression"),
                "emotion_snapshot": log.get("emotion_snapshot"),
            }
        )
    characters = [
        {
            "char_id": ch["char_id"],
            "char_name": ch.get("name") or ch["char_id"],
            "avatar_url": f"/assets/character_images/{story_id}/{ch['char_id']}/neutral.png",
        }
        for ch in snapshot["characters"]
    ]
    if includes_narrator and not any(ch["char_id"] == NARRATOR_CHAR_ID for ch in characters):
        characters.append(
            {
                "char_id": NARRATOR_CHAR_ID,
                "char_name": NARRATOR_DISPLAY_NAME,
                "avatar_url": NARRATOR_AVATAR_URL,
            }
        )
    return web.json_response({
        "status": "ok",
        "story_id": story_id,
        "action": "replay",
        "story": {"title": snapshot["story"].get("title", story_id)},
        "places": snapshot["places"],
        "characters": characters,
        "events": events,
        "count": len(events),
    })


class BgmConfigError(ValueError):
    """Raised when a BGM config request is invalid."""


class BgmUploadTooLarge(ValueError):
    """Raised when an uploaded BGM file exceeds the configured byte limit."""


def _default_bgm_manifest(story_id: str) -> dict[str, Any]:
    return {
        "story_id": story_id,
        "enabled": True,
        "story_default": None,
        "place_overrides": {},
        "mood_overrides": {},
    }


def _normalize_bgm_manifest(story_id: str, manifest: dict[str, Any]) -> dict[str, Any]:
    normalized = _default_bgm_manifest(story_id)
    normalized.update(manifest)
    if not isinstance(normalized.get("place_overrides"), dict):
        normalized["place_overrides"] = {}
    if not isinstance(normalized.get("mood_overrides"), dict):
        normalized["mood_overrides"] = {}
    normalized["story_id"] = story_id
    return normalized


def _read_bgm_manifest(story_id: str, story_maps_root: Path) -> dict[str, Any]:
    p = story_maps_root / story_id / "bgm_manifest.json"
    if p.exists():
        return _normalize_bgm_manifest(story_id, json.loads(p.read_text(encoding="utf-8")))
    return _default_bgm_manifest(story_id)


def _write_bgm_manifest(story_id: str, manifest: dict[str, Any], story_maps_root: Path) -> None:
    p = story_maps_root / story_id / "bgm_manifest.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _bgm_file_extension(filename: str | None) -> str:
    extension = Path(filename or "").suffix.lower()
    if extension not in BGM_ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(BGM_ALLOWED_EXTENSIONS))
        raise BgmConfigError(f"BGM file extension must be one of: {allowed}")
    return extension


def _validate_bgm_place_id(place_id: str) -> str:
    normalized = str(place_id or "").strip()
    if not BGM_PLACE_ID_RE.fullmatch(normalized):
        raise BgmConfigError(f"invalid place id '{place_id}'")
    return normalized


def _remove_story_bgm_files(story_dir: Path) -> None:
    for extension in BGM_ALLOWED_EXTENSIONS:
        bgm_file = story_dir / f"story_bgm{extension}"
        if bgm_file.exists():
            bgm_file.unlink()


def _remove_place_bgm_files(story_dir: Path, place_id: str) -> None:
    for extension in BGM_ALLOWED_EXTENSIONS:
        bgm_file = story_dir / "place_bgm" / f"{place_id}{extension}"
        if bgm_file.exists():
            bgm_file.unlink()


async def _read_bgm_upload_to_temp(
    field: Any,
    story_dir: Path,
    max_bytes: int,
    extension: str,
) -> Path:
    story_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".bgm-upload-",
        suffix=extension,
        dir=story_dir,
    )
    tmp_path = Path(tmp_name)
    total_bytes = 0
    try:
        with os.fdopen(fd, "wb") as tmp_file:
            while True:
                chunk = await field.read_chunk(size=BGM_UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    raise BgmUploadTooLarge(f"BGM upload exceeds {max_bytes} bytes")
                tmp_file.write(chunk)
        if total_bytes <= 0:
            raise BgmConfigError("BGM file is empty")
        return tmp_path
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _install_uploaded_bgm(tmp_path: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(tmp_path, destination)


async def _active_bgm_place_ids(request: web.Request, story_id: str) -> set[str] | None:
    payload = await request.app[PLACE_EDITING_KEY].get_edit_payload(story_id)
    if payload is None:
        return None
    return {str(place["place_id"]) for place in payload.get("places") or []}


def _story_bgm_exists(story_dir: Path, story_default: Any) -> bool:
    if not isinstance(story_default, str) or not story_default.strip():
        return False
    candidate = story_dir / story_default.strip()
    try:
        candidate.resolve().relative_to(story_dir.resolve())
    except ValueError:
        return False
    return candidate.exists()


async def get_story_bgm_config(request: web.Request) -> web.Response:
    story_id = request.match_info["story_id"]
    if await request.app[DB_KEY].get_story(story_id) is None:
        return _json_error("Story not found", 404)

    story_maps_root: Path = request.app[STORY_MAPS_ROOT_KEY]
    manifest = _read_bgm_manifest(story_id, story_maps_root)
    story_dir = story_maps_root / story_id
    return _json_ok({
        "enabled": bool(manifest.get("enabled", True)),
        "has_story_bgm": _story_bgm_exists(story_dir, manifest.get("story_default")),
        "story_default": manifest.get("story_default"),
        "place_overrides": manifest.get("place_overrides", {}),
    })


async def put_story_bgm_config(request: web.Request) -> web.Response:
    story_id = request.match_info["story_id"]
    if await request.app[DB_KEY].get_story(story_id) is None:
        return _json_error("Story not found", 404)

    story_maps_root: Path = request.app[STORY_MAPS_ROOT_KEY]
    max_bgm_upload_bytes = request.app[MAX_BGM_UPLOAD_BYTES_KEY]

    config: dict[str, Any] = {}
    uploaded_story_bgm: tuple[Path, str] | None = None
    uploaded_place_bgm: dict[str, tuple[Path, str]] = {}
    story_dir = story_maps_root / story_id
    temp_uploads: list[Path] = []

    active_place_ids = await _active_bgm_place_ids(request, story_id)
    if active_place_ids is None:
        return _json_error("Story not found", 404)

    try:
        if request.content_type.startswith("multipart/"):
            reader = await request.multipart()
            async for field in reader:
                if field.name == "config":
                    config = json.loads(await field.text())
                elif field.name == "story_bgm":
                    extension = _bgm_file_extension(field.filename)
                    tmp_path = await _read_bgm_upload_to_temp(
                        field,
                        story_dir,
                        max_bgm_upload_bytes,
                        extension,
                    )
                    temp_uploads.append(tmp_path)
                    uploaded_story_bgm = (tmp_path, extension)
                elif field.name and field.name.startswith("place_bgm__"):
                    place_id = _validate_bgm_place_id(field.name.removeprefix("place_bgm__"))
                    if place_id not in active_place_ids:
                        raise BgmConfigError(f"unknown place id '{place_id}'")
                    if place_id in uploaded_place_bgm:
                        raise BgmConfigError(f"duplicate place BGM upload for '{place_id}'")
                    extension = _bgm_file_extension(field.filename)
                    tmp_path = await _read_bgm_upload_to_temp(
                        field,
                        story_dir,
                        max_bgm_upload_bytes,
                        extension,
                    )
                    temp_uploads.append(tmp_path)
                    uploaded_place_bgm[place_id] = (tmp_path, extension)
        else:
            config = await request.json()
    except json.JSONDecodeError as exc:
        for tmp_path in temp_uploads:
            tmp_path.unlink(missing_ok=True)
        return _json_error(str(exc) or "invalid BGM config payload", 400)
    except BgmUploadTooLarge as exc:
        for tmp_path in temp_uploads:
            tmp_path.unlink(missing_ok=True)
        return _json_error(str(exc), 413)
    except BgmConfigError as exc:
        for tmp_path in temp_uploads:
            tmp_path.unlink(missing_ok=True)
        return _json_error(str(exc), 400)

    if not isinstance(config, dict):
        for tmp_path in temp_uploads:
            tmp_path.unlink(missing_ok=True)
        return _json_error("BGM config must be an object", 400)

    try:
        raw_clear_place_overrides = config.get("clear_place_overrides", [])
        if raw_clear_place_overrides is None:
            raw_clear_place_overrides = []
        if not isinstance(raw_clear_place_overrides, list):
            raise BgmConfigError("clear_place_overrides must be a list")
        clear_place_overrides = [
            _validate_bgm_place_id(place_id)
            for place_id in raw_clear_place_overrides
        ]
    except BgmConfigError as exc:
        for tmp_path in temp_uploads:
            tmp_path.unlink(missing_ok=True)
        return _json_error(str(exc), 400)

    manifest = _read_bgm_manifest(story_id, story_maps_root)
    story_dir.mkdir(parents=True, exist_ok=True)

    if "enabled" in config:
        manifest["enabled"] = bool(config["enabled"])

    if config.get("clear_story_bgm"):
        _remove_story_bgm_files(story_dir)
        manifest["story_default"] = None
    elif uploaded_story_bgm:
        tmp_path, extension = uploaded_story_bgm
        _remove_story_bgm_files(story_dir)
        _install_uploaded_bgm(tmp_path, story_dir / f"story_bgm{extension}")
        temp_uploads.remove(tmp_path)
        manifest["story_default"] = f"story_bgm{extension}"

    for place_id in clear_place_overrides:
        _remove_place_bgm_files(story_dir, place_id)
        manifest.get("place_overrides", {}).pop(place_id, None)

    if uploaded_place_bgm:
        overrides: dict[str, str] = manifest.setdefault("place_overrides", {})
        for place_id, (tmp_path, extension) in uploaded_place_bgm.items():
            _remove_place_bgm_files(story_dir, place_id)
            _install_uploaded_bgm(tmp_path, story_dir / "place_bgm" / f"{place_id}{extension}")
            temp_uploads.remove(tmp_path)
            overrides[place_id] = f"place_bgm/{place_id}{extension}"

    _write_bgm_manifest(story_id, manifest, story_maps_root)

    return _json_ok({
        "enabled": bool(manifest.get("enabled", True)),
        "has_story_bgm": _story_bgm_exists(story_dir, manifest.get("story_default")),
        "story_default": manifest.get("story_default"),
        "place_overrides": manifest.get("place_overrides", {}),
    })


async def get_story_archive(request: web.Request) -> web.Response:
    story_id = request.match_info["story_id"]
    manifest_path = request.app[ARCHIVE_ROOT_KEY] / "stories" / story_id / "manifest.json"
    if not manifest_path.exists():
        return _json_error("Archive not found", 404)
    return _json_ok(json.loads(manifest_path.read_text(encoding="utf-8")))


async def get_story_archive_page(request: web.Request) -> web.Response:
    story_id = request.match_info["story_id"]
    page = int(request.match_info["page"])
    page_path = request.app[ARCHIVE_ROOT_KEY] / "stories" / story_id / "pages" / f"{page:04d}.json"
    if not page_path.exists():
        return _json_error("Archive page not found", 404)
    return _json_ok(json.loads(page_path.read_text(encoding="utf-8")))


async def put_story_runtime(request: web.Request) -> web.Response:
    story_id = request.match_info["story_id"]
    payload = await request.json()
    desired_state = str(payload.get("desired_state", ""))
    result = await request.app[RUNTIME_KEY].set_story_state(story_id, desired_state)
    await audit(request, "story.runtime.set", story_id=story_id, result="ok", payload_summary=desired_state)
    return _json_ok(result)


async def restart_story_runtime(request: web.Request) -> web.Response:
    story_id = request.match_info["story_id"]
    result = await request.app[RUNTIME_KEY].restart_story(story_id)
    await audit(request, "story.runtime.restart", story_id=story_id, result="ok", payload_summary="restart")
    return _json_ok(result)


async def reset_story_runtime(request: web.Request) -> web.Response:
    db = request.app[DB_KEY]
    story_id = request.match_info["story_id"]
    story = await db.get_story(story_id)
    if story is None:
        return _json_error("Story not found", 404)
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return _json_error("payload must be JSON", 400)
    if payload.get("confirm") is not True:
        return _json_error("confirm must be true", 400)

    runtime_state = request.app[RUNTIME_KEY].list_states().get(story_id, "stopped")
    if runtime_state == "running":
        return _json_error("Story must be stopped before resetting progress", 409)

    try:
        result = await request.app[STORY_PROGRESS_RESET_KEY].reset(story_id)
    except StoryProgressResetError as exc:
        return _json_error(str(exc), 500)
    await audit(
        request,
        "story.runtime.reset",
        story_id=story_id,
        result="ok",
        payload_summary=json.dumps(result, ensure_ascii=False, sort_keys=True),
    )
    return _json_ok(result)


async def put_story_publication(request: web.Request) -> web.Response:
    db = request.app[DB_KEY]
    story_id = request.match_info["story_id"]
    payload = await request.json()
    visibility = str(payload.get("visibility", "draft"))
    published_at = datetime.now(UTC).isoformat() if visibility == "public" else None
    row = await db.upsert_story_publication(story_id, visibility=visibility, published_at=published_at)
    await audit(request, "story.publication.set", story_id=story_id, result="ok", payload_summary=visibility)
    return _json_ok(row)


async def post_story_archive_build(request: web.Request) -> web.Response:
    story_id = request.match_info["story_id"]
    builder = request.app[ARCHIVE_BUILDER_KEY]
    result = await builder.build_story_archive(story_id)
    await builder.build_catalog()
    await audit(request, "story.archive.build", story_id=story_id, result="ok", payload_summary="manual")
    return _json_ok({"story_id": result.story_id, "page_count": result.page_count, "scene_count": result.scene_count})


async def download_story(request: web.Request) -> web.Response:
    db = request.app[DB_KEY]
    story_id = request.match_info["story_id"]
    logs = await db.get_chat_logs(story_id)
    publication = await db.get_story_publication(story_id)
    manifest_path = request.app[ARCHIVE_ROOT_KEY] / "stories" / story_id / "manifest.json"
    manifest = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "chat_logs.jsonl",
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in logs),
        )
        zf.writestr(
            "novel_summary.json",
            json.dumps({"story_id": story_id, "publication": publication}, ensure_ascii=False, indent=2),
        )
        zf.writestr(
            "archive_manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
    return web.Response(
        body=buffer.getvalue(),
        headers={
            "Content-Type": "application/zip",
            "Content-Disposition": f'attachment; filename="{story_id}.zip"',
        },
    )


async def get_conversation_patterns(request: web.Request) -> web.Response:
    db = request.app[DB_KEY]
    story_id = request.match_info["story_id"]
    story = await db.get_story(story_id)
    if story is None:
        return _json_error("Story not found", 404)
    patterns = await db.get_conversation_motif_settings(story_id)
    return _json_ok({"story_id": story_id, "patterns": patterns})


async def put_conversation_patterns(request: web.Request) -> web.Response:
    db = request.app[DB_KEY]
    story_id = request.match_info["story_id"]
    story = await db.get_story(story_id)
    if story is None:
        return _json_error("Story not found", 404)
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return _json_error("payload must be JSON", 400)
    patterns = payload.get("patterns")
    if not isinstance(patterns, list):
        return _json_error("patterns must be a list", 400)
    normalized: list[dict[str, Any]] = []
    for item in patterns:
        if not isinstance(item, dict):
            return _json_error("pattern item must be an object", 400)
        motif_id = str(item.get("motif_id") or "").strip()
        if motif_id != "solo_seed_rondo":
            return _json_error(f"unsupported motif_id: {motif_id}", 400)
        strength = str(item.get("strength") or "moderate").strip()
        if strength not in {"subtle", "moderate", "strong"}:
            return _json_error("strength must be subtle, moderate, or strong", 400)
        try:
            cooldown_turns = int(item.get("cooldown_turns", 4))
        except (TypeError, ValueError):
            return _json_error("cooldown_turns must be an integer", 400)
        if cooldown_turns < 0 or cooldown_turns > 50:
            return _json_error("cooldown_turns must be between 0 and 50", 400)
        normalized.append(
            {
                "motif_id": motif_id,
                "enabled": bool(item.get("enabled")),
                "strength": strength,
                "cooldown_turns": cooldown_turns,
            }
        )
    await db.upsert_conversation_motif_settings(story_id, normalized)
    updated = await db.get_conversation_motif_settings(story_id)
    await audit(
        request,
        "story.conversation_patterns.set",
        story_id=story_id,
        result="ok",
        payload_summary=json.dumps(normalized, ensure_ascii=False, sort_keys=True),
    )
    return _json_ok({"story_id": story_id, "patterns": updated})


async def get_scene_scripts(request: web.Request) -> web.Response:
    """GET /admin/api/v1/stories/{story_id}/scene-scripts?limit=30"""
    db = request.app[DB_KEY]
    story_id = request.match_info["story_id"]
    try:
        limit = min(int(request.rel_url.query.get("limit", "30")), 200)
    except (TypeError, ValueError):
        limit = 30
    scripts = await db.get_recent_scene_scripts(story_id, limit=limit)
    return _json_ok({"scripts": scripts})


async def delete_scene_scripts_from_turn_handler(request: web.Request) -> web.Response:
    """DELETE /admin/api/v1/stories/{story_id}/scene-scripts/from-turn/{turn}"""
    db = request.app[DB_KEY]
    story_id = request.match_info["story_id"]
    try:
        turn = int(request.match_info["turn"])
    except (TypeError, ValueError):
        return _json_error("turn must be a positive integer", 400)
    if turn < 1:
        return _json_error("turn must be >= 1", 400)
    deleted = await db.delete_scene_scripts_from_turn(story_id, turn)
    await audit(
        request,
        "story.scene_scripts.reset",
        story_id=story_id,
        result="ok",
        payload_summary=f"from_turn={turn} deleted={deleted}",
    )
    return _json_ok({"deleted": deleted})


async def get_settings(request: web.Request) -> web.Response:
    db = request.app[DB_KEY]
    value = await db.get_system_setting("archive_publish_interval_minutes", default="5")
    try:
        llm_runtime = await request.app[LLM_RUNTIME_SETTINGS_KEY].get_payload()
    except LLMRuntimeSettingsError as exc:
        llm_runtime = {"error": str(exc)}
    return _json_ok(
        {
            "archive_publish_interval_minutes": int(value),
            "llm_runtime": llm_runtime,
        }
    )


async def put_archive_settings(request: web.Request) -> web.Response:
    db = request.app[DB_KEY]
    payload = await request.json()
    interval = int(payload.get("interval_minutes", 5))
    await db.set_system_setting("archive_publish_interval_minutes", str(interval))
    await audit(request, "settings.archive_publish.set", story_id=None, result="ok", payload_summary=str(interval))
    return _json_ok({"archive_publish_interval_minutes": interval})


async def put_llm_runtime_settings(request: web.Request) -> web.Response:
    payload = await request.json()
    service = request.app[LLM_RUNTIME_SETTINGS_KEY]
    try:
        result = await service.update_settings(payload)
    except LLMRuntimeSettingsError as exc:
        return _json_error(str(exc), 400)

    apply_runtime_config = getattr(request.app[RUNTIME_KEY], "apply_llm_runtime_config", None)
    applied_to_runtime = False
    if callable(apply_runtime_config):
        apply_runtime_config(result["runtime_config"])
        applied_to_runtime = True

    await audit(
        request,
        "settings.llm_runtime.set",
        story_id=None,
        result="ok",
        payload_summary=(
            f"{result['profile_count']} profiles, {result['story_override_count']} overrides"
        ),
    )
    result_payload = {
        "path": result["path"],
        "active_profile": result["active_profile"],
        "profile_count": result["profile_count"],
        "story_override_count": result["story_override_count"],
        "applied_to_runtime": applied_to_runtime,
        "running_story_restart_required": True,
    }
    return _json_ok(result_payload)


# ── Web 投稿先設定 ────────────────────────────────────────────────────────────

async def get_settings_web_post(request: web.Request) -> web.Response:
    """グローバルデフォルト Web 投稿先設定を返す。"""
    db = request.app[DB_KEY]
    receiver_url = await db.get_system_setting("default_web_receiver_url") or ""
    auth_token = await db.get_system_setting("default_web_auth_token") or ""
    return _json_ok(_web_post_response(receiver_url=receiver_url, auth_token=auth_token))


async def put_settings_web_post(request: web.Request) -> web.Response:
    """グローバルデフォルト Web 投稿先設定を更新する。"""
    db = request.app[DB_KEY]
    payload = await request.json() if request.content_length else {}
    current_url = await db.get_system_setting("default_web_receiver_url") or ""
    current_token = await db.get_system_setting("default_web_auth_token") or ""

    url = str(payload.get("receiver_url", current_url)).strip()
    validated_url = _validate_receiver_url_for_request(url)
    if isinstance(validated_url, web.Response):
        return validated_url
    url = validated_url

    if bool(payload.get("clear_auth_token", False)):
        token = ""
    else:
        raw_token = str(payload.get("auth_token", "")).strip() if "auth_token" in payload else ""
        token = raw_token if raw_token else current_token

    await db.set_system_setting("default_web_receiver_url", url)
    await db.set_system_setting("default_web_auth_token",   token)
    await audit(
        request, "settings.web_post.set", story_id=None, result="ok",
        payload_summary=f"url={url[:40] if url else '(empty)'}",
    )
    return _json_ok(_web_post_response(receiver_url=url, auth_token=token))


async def get_story_web_post(request: web.Request) -> web.Response:
    """per-story の Web 投稿先設定を返す。"""
    db = request.app[DB_KEY]
    story_id = request.match_info["story_id"]
    if not await _story_exists(db, story_id):
        return _json_error("story not found", 404)
    settings = await db.get_story_web_post_settings(story_id)
    return _json_ok(
        _web_post_response(
            story_id=story_id,
            receiver_url=settings["receiver_url"],
            auth_token=settings["auth_token"],
        )
    )


async def put_story_web_post(request: web.Request) -> web.Response:
    """per-story の Web 投稿先設定を更新する。空文字でデフォルト使用に戻す。"""
    db = request.app[DB_KEY]
    story_id = request.match_info["story_id"]
    if not await _story_exists(db, story_id):
        return _json_error("story not found", 404)
    payload = await request.json() if request.content_length else {}
    current = await db.get_story_web_post_settings(story_id)
    url = str(payload.get("receiver_url", current["receiver_url"])).strip()
    validated_url = _validate_receiver_url_for_request(url)
    if isinstance(validated_url, web.Response):
        return validated_url
    url = validated_url

    if bool(payload.get("clear_auth_token", False)):
        token = ""
    else:
        raw_token = str(payload.get("auth_token", "")).strip() if "auth_token" in payload else ""
        token = raw_token if raw_token else current["auth_token"]

    updated = await db.set_story_web_post_settings(story_id, url, token)
    if not updated:
        return _json_error("story not found", 404)
    await audit(
        request, "story.web_post.set", story_id=story_id, result="ok",
        payload_summary=f"url={url[:40] if url else '(empty)'}",
    )
    return _json_ok(_web_post_response(story_id=story_id, receiver_url=url, auth_token=token))


async def get_audit_logs(request: web.Request) -> web.Response:
    db = request.app[DB_KEY]
    params = request.rel_url.query
    try:
        limit = int(params.get("limit", "50"))
        offset = int(params.get("offset", "0"))
    except ValueError:
        return _json_error("limit and offset must be integers", 400)
    if limit < 1:
        return _json_error("limit must be greater than 0", 400)
    if offset < 0:
        return _json_error("offset must be greater than or equal to 0", 400)
    limit = min(limit, 200)
    story_id = params.get("story_id") or None
    action = params.get("action") or None
    rows = await db.list_admin_audit_logs(
        limit=limit, offset=offset, story_id=story_id, action=action
    )
    return _json_ok({"logs": rows, "limit": limit, "offset": offset})


async def audit(
    request: web.Request,
    action: str,
    story_id: str | None,
    result: str,
    payload_summary: str | None,
) -> None:
    actor = request.get("actor", {"actor_type": "unknown", "actor_label": "unknown"})
    db = request.app[DB_KEY]
    await db.insert_admin_audit_log(
        actor_type=actor["actor_type"],
        actor_label=actor["actor_label"],
        action=action,
        story_id=story_id,
        result=result,
        payload_summary=payload_summary,
    )


# ── Chapter Proposals（Phase 4）───────────────────────────────────────────────

async def _chapter_with_beats(
    db: DatabaseManager,
    chapter: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if chapter is None:
        return None
    payload = dict(chapter)
    payload["beats"] = await db.get_chapter_beats(int(chapter["id"]))
    return payload


async def get_story_chapters(request: web.Request) -> web.Response:
    """story の chapter 管理画面向け overview を返す。"""
    db = request.app[DB_KEY]
    story_id = request.match_info["story_id"]
    if await db.get_story(story_id) is None:
        return _json_error("Story not found", 404)

    active = await _chapter_with_beats(db, await db.get_active_chapter(story_id))
    pending = [
        await _chapter_with_beats(db, chapter)
        for chapter in await db.get_pending_chapters(story_id)
    ]
    closed = [
        await _chapter_with_beats(db, chapter)
        for chapter in await db.get_closed_chapters(story_id, limit=10)
    ]
    return _json_ok(
        {
            "story_id": story_id,
            "runtime_state": request.app[RUNTIME_KEY].list_states().get(
                story_id,
                "stopped",
            ),
            "active_director_persona": await db.get_active_director_persona(story_id),
            "director_personas": await db.get_all_director_personas(story_id),
            "director_swap_history": await db.get_director_swap_history(story_id),
            "active_chapter": active,
            "pending_chapters": [chapter for chapter in pending if chapter is not None],
            "closed_chapters": [chapter for chapter in closed if chapter is not None],
        }
    )


async def get_chapter_proposals(request: web.Request) -> web.Response:
    """pending な chapter proposal の一覧を返す。"""
    db = request.app[DB_KEY]
    story_id = request.match_info["story_id"]
    status_filter = request.rel_url.query.get("status", "pending")
    proposals = await db.get_chapter_proposals(
        story_id, status=status_filter if status_filter else None
    )
    return _json_ok({"proposals": proposals})


async def post_generate_chapter_proposal(request: web.Request) -> web.Response:
    """LLM を使って chapter proposal を生成して DB に保存する。"""
    from engine.chapter_generator import ChapterGenerationError

    db = request.app[DB_KEY]
    story_id = request.match_info["story_id"]
    payload = await request.json()
    theme = str(payload.get("theme", "")).strip()
    if not theme:
        return _json_error("theme is required", 400)
    turn_number = int(payload.get("turn_number", 0))

    runtime = request.app[RUNTIME_KEY]
    chapter_generator = runtime.get_chapter_generator(story_id)
    if chapter_generator is None:
        return _json_error(
            "chapter_generator is not available for this story "
            "(engine not running or chapter_generator.enabled=false)",
            503,
        )

    # active persona を取得してプロンプトに反映（optional）
    persona = await db.get_active_director_persona(story_id)

    try:
        proposal = await chapter_generator.generate_proposal(
            theme, turn_number, persona=persona
        )
    except ChapterGenerationError as exc:
        return _json_error(f"Chapter generation failed: {exc}", 422)

    await audit(
        request, "chapter.proposal.generate",
        story_id=story_id, result="ok", payload_summary=theme,
    )
    return _json_ok({"proposal": proposal}, status=201)


async def put_approve_chapter_proposal(request: web.Request) -> web.Response:
    """proposal を承認して pending chapter に変換する。"""
    db = request.app[DB_KEY]
    story_id = request.match_info["story_id"]
    proposal_id = int(request.match_info["proposal_id"])

    proposal = await db.get_chapter_proposal(proposal_id)
    if proposal is None or proposal["story_id"] != story_id:
        return _json_error("Proposal not found", 404)
    if proposal["admin_status"] != "pending":
        return _json_error(f"Proposal is already {proposal['admin_status']}", 409)

    payload = await request.json() if request.content_length else {}
    turn_number = int(payload.get("turn_number", 0))
    notes = str(payload.get("notes", "")).strip() or None

    chapter_dict: dict[str, Any] = proposal["proposed_chapter_json"]
    beats: list[dict[str, Any]] = chapter_dict.pop("beats", [])

    chapter_db_id = await db.insert_chapter(story_id, {
        "chapter_id": chapter_dict.get("chapter_id", f"proposal_{proposal_id}"),
        "title": chapter_dict.get("title", ""),
        "theme": chapter_dict.get("theme", ""),
        "world_injection": chapter_dict.get("world_injection", ""),
        "status": "pending",
        "start_condition": chapter_dict.get("start_condition", "manual"),
        "current_beat": "setup",
    })
    for beat in beats:
        await db.insert_chapter_beat(chapter_db_id, {
            "phase": beat.get("phase", "setup"),
            "description": beat.get("description", ""),
            "goal": beat.get("goal", ""),
            "events_json": beat.get("events_json", []),
        })

    await db.update_chapter_proposal_status(
        proposal_id, "approved", approved_turn=turn_number, notes=notes
    )
    await audit(
        request, "chapter.proposal.approve",
        story_id=story_id, result="ok", payload_summary=str(proposal_id),
    )
    return _json_ok({"chapter_db_id": chapter_db_id, "proposal_id": proposal_id}, status=201)


async def put_reject_chapter_proposal(request: web.Request) -> web.Response:
    """proposal を却下する。"""
    db = request.app[DB_KEY]
    story_id = request.match_info["story_id"]
    proposal_id = int(request.match_info["proposal_id"])

    proposal = await db.get_chapter_proposal(proposal_id)
    if proposal is None or proposal["story_id"] != story_id:
        return _json_error("Proposal not found", 404)
    if proposal["admin_status"] != "pending":
        return _json_error(f"Proposal is already {proposal['admin_status']}", 409)

    payload = await request.json() if request.content_length else {}
    notes = str(payload.get("notes", "")).strip() or None

    await db.update_chapter_proposal_status(proposal_id, "rejected", notes=notes)
    await audit(
        request, "chapter.proposal.reject",
        story_id=story_id, result="ok", payload_summary=str(proposal_id),
    )
    return _json_ok({"proposal_id": proposal_id, "admin_status": "rejected"})


async def put_activate_director_persona(request: web.Request) -> web.Response:
    """active director persona を切り替える。"""
    db = request.app[DB_KEY]
    story_id = request.match_info["story_id"]
    persona_id = request.match_info["persona_id"]

    if await db.get_story(story_id) is None:
        return _json_error("Story not found", 404)

    personas = await db.get_all_director_personas(story_id)
    target = next((persona for persona in personas if persona["persona_id"] == persona_id), None)
    if target is None:
        return _json_error("Director persona not found", 404)
    active = next((persona for persona in personas if int(persona.get("is_active", 0)) == 1), None)
    from_persona_id = active["persona_id"] if active is not None else None
    if from_persona_id == persona_id:
        return _json_error(f"Director persona is already active: {persona_id}", 409)

    payload = await request.json() if request.content_length else {}
    if "turn_number" not in payload:
        return _json_error("turn_number is required", 400)
    turn_number = int(payload["turn_number"])
    reason = str(payload.get("reason", "")).strip() or None

    await db.set_persona_active(story_id, persona_id)
    await db.insert_director_swap_log(
        story_id,
        {
            "turn_number": turn_number,
            "from_persona_id": from_persona_id,
            "to_persona_id": persona_id,
            "reason": reason,
        },
    )
    runtime_reloaded = await request.app[RUNTIME_KEY].reload_director_persona(story_id)
    await audit(
        request,
        "director.persona.activate",
        story_id=story_id,
        result="ok",
        payload_summary=persona_id,
    )
    return _json_ok(
        {
            "from_persona_id": from_persona_id,
            "to_persona_id": persona_id,
            "runtime_reloaded": runtime_reloaded,
        }
    )


async def post_activate_chapter(request: web.Request) -> web.Response:
    """pending/manual chapter を active にする。"""
    db = request.app[DB_KEY]
    story_id = request.match_info["story_id"]
    chapter_db_id = int(request.match_info["chapter_db_id"])

    chapter = await db.get_story_chapter(chapter_db_id)
    if chapter is None or chapter["story_id"] != story_id:
        return _json_error("Chapter not found", 404)
    if chapter["status"] != "pending":
        return _json_error(f"Chapter is {chapter['status']}, not pending", 409)
    if await db.get_active_chapter(story_id) is not None:
        return _json_error("An active chapter already exists", 409)

    payload = await request.json() if request.content_length else {}
    if "turn_number" not in payload:
        return _json_error("turn_number is required", 400)
    turn_number = int(payload["turn_number"])

    await db.activate_chapter(chapter_db_id, opened_turn=turn_number)
    await audit(
        request,
        "chapter.activate",
        story_id=story_id,
        result="ok",
        payload_summary=str(chapter_db_id),
    )
    return _json_ok(
        {
            "chapter_db_id": chapter_db_id,
            "status": "active",
            "opened_turn": turn_number,
        },
        status=201,
    )


async def post_close_chapter(request: web.Request) -> web.Response:
    """active chapter を手動で closed にする。"""
    db = request.app[DB_KEY]
    story_id = request.match_info["story_id"]
    chapter_db_id = int(request.match_info["chapter_db_id"])

    chapter = await db.get_story_chapter(chapter_db_id)
    if chapter is None or chapter["story_id"] != story_id:
        return _json_error("Chapter not found", 404)
    if chapter["status"] != "active":
        return _json_error(f"Chapter is {chapter['status']}, not active", 409)

    payload = await request.json() if request.content_length else {}
    if "turn_number" not in payload:
        return _json_error("turn_number is required", 400)
    turn_number = int(payload["turn_number"])
    reason = str(payload.get("reason", "")).strip() or "manual_admin"
    carry_over = payload.get("carry_over", {})
    if not isinstance(carry_over, dict):
        return _json_error("carry_over must be an object", 400)

    await db.close_chapter(
        chapter_db_id,
        closed_turn=turn_number,
        reason=reason,
        carry_over=carry_over,
    )
    await audit(
        request,
        "chapter.close",
        story_id=story_id,
        result="ok",
        payload_summary=str(chapter_db_id),
    )
    return _json_ok(
        {
            "chapter_db_id": chapter_db_id,
            "status": "closed",
            "closed_turn": turn_number,
            "close_reason": reason,
        }
    )


async def post_reopen_chapter(request: web.Request) -> web.Response:
    """closed chapter を active に戻す。"""
    db = request.app[DB_KEY]
    story_id = request.match_info["story_id"]
    chapter_db_id = int(request.match_info["chapter_db_id"])

    chapter = await db.get_story_chapter(chapter_db_id)
    if chapter is None or chapter["story_id"] != story_id:
        return _json_error("Chapter not found", 404)
    if chapter["status"] != "closed":
        return _json_error(f"Chapter is {chapter['status']}, not closed", 409)
    if await db.get_active_chapter(story_id) is not None:
        return _json_error("An active chapter already exists", 409)

    payload = await request.json() if request.content_length else {}
    if "turn_number" not in payload:
        return _json_error("turn_number is required", 400)
    turn_number = int(payload["turn_number"])

    await db.reopen_chapter(chapter_db_id, opened_turn=turn_number)
    await audit(
        request,
        "chapter.reopen",
        story_id=story_id,
        result="ok",
        payload_summary=str(chapter_db_id),
    )
    return _json_ok(
        {
            "chapter_db_id": chapter_db_id,
            "status": "active",
            "opened_turn": turn_number,
        }
    )


async def put_chapter_current_beat(request: web.Request) -> web.Response:
    """active chapter の current_beat を手動で更新する。"""
    db = request.app[DB_KEY]
    story_id = request.match_info["story_id"]
    chapter_db_id = int(request.match_info["chapter_db_id"])

    chapter = await db.get_story_chapter(chapter_db_id)
    if chapter is None or chapter["story_id"] != story_id:
        return _json_error("Chapter not found", 404)
    if chapter["status"] != "active":
        return _json_error(f"Chapter is {chapter['status']}, not active", 409)

    payload = await request.json() if request.content_length else {}
    current_beat = str(payload.get("current_beat") or "").strip()
    if not current_beat:
        return _json_error("current_beat is required", 400)

    beats = await db.get_chapter_beats(chapter_db_id)
    valid_phases = {str(beat.get("phase") or "") for beat in beats}
    if current_beat not in valid_phases:
        return _json_error(f"unknown beat phase: {current_beat}", 400)

    await db.update_chapter_beat(chapter_db_id, current_beat)
    await audit(
        request,
        "chapter.current_beat.set",
        story_id=story_id,
        result="ok",
        payload_summary=f"{chapter_db_id}:{current_beat}",
    )
    return _json_ok(
        {
            "chapter_db_id": chapter_db_id,
            "status": "active",
            "current_beat": current_beat,
        }
    )


# ── 発話文字数設定（per-story） ───────────────────────────────────────────────

_VALID_MAX_CHARS = frozenset({180, 240, 320})
_VALID_MAX_SENTENCES = frozenset({3, 4, 5})


async def get_story_utterance_settings(request: web.Request) -> web.Response:
    """per-story の発話長設定（文字数・文数）を返す。"""
    db = request.app[DB_KEY]
    story_id = request.match_info["story_id"]
    if not await _story_exists(db, story_id):
        return _json_error("story not found", 404)
    settings = await db.get_utterance_settings(story_id)
    return _json_ok(settings)


async def put_story_utterance_settings(request: web.Request) -> web.Response:
    """per-story の発話長設定を更新する。max_chars / max_sentences は個別・同時どちらも可。"""
    db = request.app[DB_KEY]
    story_id = request.match_info["story_id"]
    if not await _story_exists(db, story_id):
        return _json_error("story not found", 404)
    payload = await request.json() if request.content_length else {}

    max_chars = payload.get("max_chars")
    max_sentences = payload.get("max_sentences")

    if max_chars is not None:
        try:
            max_chars = int(max_chars)
        except (ValueError, TypeError):
            return _json_error("max_chars は整数が必要です", 400)
        if max_chars not in _VALID_MAX_CHARS:
            return _json_error(f"max_chars は {sorted(_VALID_MAX_CHARS)} のいずれかが必要です", 400)

    if max_sentences is not None:
        try:
            max_sentences = int(max_sentences)
        except (ValueError, TypeError):
            return _json_error("max_sentences は整数が必要です", 400)
        if max_sentences not in _VALID_MAX_SENTENCES:
            return _json_error(f"max_sentences は {sorted(_VALID_MAX_SENTENCES)} のいずれかが必要です", 400)

    if max_chars is None and max_sentences is None:
        return _json_error("max_chars または max_sentences のいずれかが必要です", 400)

    await db.upsert_utterance_settings(story_id, max_chars=max_chars, max_sentences=max_sentences)

    summary_parts = []
    if max_chars is not None:
        summary_parts.append(f"max_chars={max_chars}")
    if max_sentences is not None:
        summary_parts.append(f"max_sentences={max_sentences}")
    await audit(
        request,
        "utterance.settings.set",
        story_id=story_id,
        result="ok",
        payload_summary=", ".join(summary_parts),
    )
    settings = await db.get_utterance_settings(story_id)
    return _json_ok({"story_id": story_id, **settings})


# ── 時事モード: RSS フィード管理 ──────────────────────────────────────────────

_VALID_INTENSITIES = frozenset({
    "off",
    "low",
    "medium",
    "high",
    "rate20",
    "rate30",
    "rate40",
    "rate50",
    "rate80",
})


def _validate_news_feed_url_for_request(request: web.Request, url: str) -> str | web.Response:
    config: NewsModeConfig = request.app[NEWS_MODE_CONFIG_KEY]
    try:
        return validate_feed_url(
            url,
            allow_private_hosts=config.allow_private_feed_hosts,
        )
    except FeedUrlValidationError as exc:
        return _json_error(f"invalid feed url: {exc}", 400)


async def get_news_feeds(request: web.Request) -> web.Response:
    """RSS フィード一覧を返す。"""
    store: NewsArticleStore = request.app[NEWS_STORE_KEY]
    feeds = await store.list_feeds()
    count = await store.count_articles()
    return _json_ok({"feeds": feeds, "total_articles": count})


_MAX_TAGS_PER_ENTITY = 20
_MAX_TAG_LENGTH = 50


def _parse_tag_names(raw: Any) -> tuple[list[str], str | None]:
    """payload の tags フィールドをパース・バリデートして (list[str], error|None) を返す。"""
    import re as _re
    if raw is None:
        return [], None
    if not isinstance(raw, list):
        return [], "tags は配列が必要です"
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            return [], "tags の各要素は文字列が必要です"
        name = " ".join(item.strip().split())
        if not name:
            continue
        if len(name) > _MAX_TAG_LENGTH:
            return [], f"タグ名は {_MAX_TAG_LENGTH} 字以内にしてください: {name!r}"
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(name)
    if len(result) > _MAX_TAGS_PER_ENTITY:
        return [], f"タグは {_MAX_TAGS_PER_ENTITY} 個以内にしてください"
    return result, None


async def get_news_tags(request: web.Request) -> web.Response:
    """登録済みタグ名の一覧を返す（autocomplete 用）。"""
    store: NewsArticleStore = request.app[NEWS_STORE_KEY]
    tags = await store.list_all_tags()
    return _json_ok({"tags": tags})


async def post_news_feed(request: web.Request) -> web.Response:
    """RSS フィードを追加する。tags 配列を渡すとタグも登録する。"""
    store: NewsArticleStore = request.app[NEWS_STORE_KEY]
    payload = await request.json() if request.content_length else {}
    url = str(payload.get("url") or "").strip()
    validated_url = _validate_news_feed_url_for_request(request, url)
    if isinstance(validated_url, web.Response):
        return validated_url
    title = str(payload.get("title") or "").strip() or None
    enabled = bool(payload.get("enabled", True))
    tag_names, tag_err = _parse_tag_names(payload.get("tags"))
    if tag_err:
        return _json_error(tag_err, 400)
    feed_id = await store.upsert_feed(validated_url, title, enabled)
    if tag_names:
        await store.set_feed_tags(feed_id, tag_names)
    await audit(request, "news.feed.add", story_id=None, result="ok", payload_summary=validated_url)
    return _json_ok({"feed_id": feed_id, "url": validated_url, "tags": tag_names})


async def put_news_feed(request: web.Request) -> web.Response:
    """RSS フィードを更新する。tags を渡すとタグも置換更新する。"""
    store: NewsArticleStore = request.app[NEWS_STORE_KEY]
    feed_id = int(request.match_info["feed_id"])
    payload = await request.json() if request.content_length else {}
    url = str(payload.get("url") or "").strip() or None
    title = str(payload.get("title") or "").strip() or None
    enabled: bool | None = None
    if "enabled" in payload:
        enabled = bool(payload["enabled"])
    if url is not None:
        validated_url = _validate_news_feed_url_for_request(request, url)
        if isinstance(validated_url, web.Response):
            return validated_url
        url = validated_url
    updated = await store.update_feed(feed_id, url=url, title=title, enabled=enabled)
    if not updated:
        return _json_error("feed not found", 404)
    if "tags" in payload:
        tag_names, tag_err = _parse_tag_names(payload.get("tags"))
        if tag_err:
            return _json_error(tag_err, 400)
        await store.set_feed_tags(feed_id, tag_names)
    await audit(request, "news.feed.update", story_id=None, result="ok", payload_summary=str(feed_id))
    return _json_ok({"feed_id": feed_id})


async def delete_news_feed(request: web.Request) -> web.Response:
    """RSS フィードを削除する（記事も CASCADE で削除）。"""
    store: NewsArticleStore = request.app[NEWS_STORE_KEY]
    feed_id = int(request.match_info["feed_id"])
    deleted = await store.delete_feed(feed_id)
    if not deleted:
        return _json_error("feed not found", 404)
    await audit(request, "news.feed.delete", story_id=None, result="ok", payload_summary=str(feed_id))
    return _json_ok({"feed_id": feed_id, "deleted": True})


async def post_news_fetch_now(request: web.Request) -> web.Response:
    """全有効フィードを手動で即時巡回する。"""
    store: NewsArticleStore = request.app[NEWS_STORE_KEY]
    # 巡回は NewsFetcher 経由でなくても良い。ここでは軽量版を直接実行。
    from engine.news_fetcher import NewsFetcher
    fetcher = NewsFetcher(store, request.app[NEWS_MODE_CONFIG_KEY])
    inserted = await fetcher.fetch_all_now()
    await audit(request, "news.fetch_now", story_id=None, result="ok", payload_summary=f"inserted={inserted}")
    return _json_ok({"inserted": inserted})


async def get_news_articles(request: web.Request) -> web.Response:
    """蓄積記事の一覧を返す（最新 100 件）。"""
    store: NewsArticleStore = request.app[NEWS_STORE_KEY]
    limit = min(int(request.rel_url.query.get("limit", "100")), 200)
    articles = await store.get_recent_articles(limit=limit)
    total = await store.count_articles()
    return _json_ok({"articles": articles, "total": total, "returned": len(articles)})


async def post_news_reset_articles(request: web.Request) -> web.Response:
    """記事のみリセット（フィードは保持）。"""
    store: NewsArticleStore = request.app[NEWS_STORE_KEY]
    await store.reset_articles_only()
    await audit(request, "news.reset.articles", story_id=None, result="ok", payload_summary=None)
    return _json_ok({"reset": "articles"})


async def post_news_reset_all(request: web.Request) -> web.Response:
    """記事とフィードをすべてリセット。"""
    store: NewsArticleStore = request.app[NEWS_STORE_KEY]
    await store.reset_all()
    await audit(request, "news.reset.all", story_id=None, result="ok", payload_summary=None)
    return _json_ok({"reset": "all"})


async def get_story_news_mode(request: web.Request) -> web.Response:
    """per-story 時事モード設定を返す。"""
    db = request.app[DB_KEY]
    story_id = request.match_info["story_id"]
    if not await _story_exists(db, story_id):
        return _json_error("story not found", 404)
    settings = await db.get_news_mode_settings(story_id)
    return _json_ok(settings)


async def put_story_news_mode(request: web.Request) -> web.Response:
    """per-story 時事モード設定を更新する。"""
    db = request.app[DB_KEY]
    story_id = request.match_info["story_id"]
    if not await _story_exists(db, story_id):
        return _json_error("story not found", 404)
    payload = await request.json() if request.content_length else {}
    enabled = bool(payload.get("enabled", False))
    intensity = str(payload.get("intensity", "low")).strip()
    if intensity not in _VALID_INTENSITIES:
        return _json_error(f"intensity は {sorted(_VALID_INTENSITIES)} のいずれかが必要です", 400)
    await db.upsert_news_mode_settings(story_id, enabled, intensity)
    await audit(
        request, "news.mode.set", story_id=story_id,
        result="ok", payload_summary=f"enabled={enabled} intensity={intensity}"
    )
    return _json_ok({"story_id": story_id, "enabled": enabled, "intensity": intensity})


async def get_story_news_tag_filter(request: web.Request) -> web.Response:
    """per-story 時事モードタグフィルタを返す。"""
    db = request.app[DB_KEY]
    story_id = request.match_info["story_id"]
    if not await _story_exists(db, story_id):
        return _json_error("story not found", 404)
    tags = await db.get_news_tag_filter(story_id)
    return _json_ok({"story_id": story_id, "tags": tags})


async def put_story_news_tag_filter(request: web.Request) -> web.Response:
    """per-story 時事モードタグフィルタを更新する。tags: [] で全削除（フィルタなし）。"""
    db = request.app[DB_KEY]
    story_id = request.match_info["story_id"]
    if not await _story_exists(db, story_id):
        return _json_error("story not found", 404)
    payload = await request.json() if request.content_length else {}
    tag_names, tag_err = _parse_tag_names(payload.get("tags", []))
    if tag_err:
        return _json_error(tag_err, 400)
    await db.set_news_tag_filter(story_id, tag_names)
    await audit(
        request, "news.tag_filter.set", story_id=story_id,
        result="ok", payload_summary=f"tags={tag_names}",
    )
    return _json_ok({"story_id": story_id, "tags": tag_names})

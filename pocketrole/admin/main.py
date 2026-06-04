"""Hosted admin service entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime
import os
from pathlib import Path

from aiohttp import web

from admin.app import (
    ARCHIVE_BUILDER_KEY,
    DB_KEY,
    PUBLISHER_STATE_KEY,
    create_app,
)
from admin.runtime_controller import HostedRuntimeController
from db.db_manager import DatabaseManager
from engine.config import load_config
from engine.llm.router import LLMRouter
from engine.llm_runtime import (
    ensure_story_llm_runtime_requirements,
    required_story_llm_providers,
)
from engine.main import setup_logging


MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"


@dataclass(frozen=True)
class HostedAdminSettings:
    host: str
    port: int
    config: str
    db: str
    stories: list[str] | None
    archive_root: str
    admin_user: str
    admin_password: str
    cookie_secure: bool


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PocketRole hosted admin service")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--config", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--stories", nargs="*", default=None)
    parser.add_argument("--archive-root", default=None)
    parser.add_argument("--admin-user", default=None)
    parser.add_argument("--admin-password", default=None)
    parser.add_argument("--cookie-secure", dest="cookie_secure", action="store_true", default=None)
    parser.add_argument("--insecure-cookie", dest="cookie_secure", action="store_false")
    return parser.parse_args(argv)


def _env_bool(env: dict[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def resolve_settings(
    args: argparse.Namespace,
    env: dict[str, str] | None = None,
) -> HostedAdminSettings:
    source = env if env is not None else dict(os.environ)
    host = args.host or source.get("POCKETROLE_ADMIN_HOST") or "127.0.0.1"
    port = args.port or int(source.get("POCKETROLE_ADMIN_PORT", "8787"))
    config = args.config or source.get("POCKETROLE_ADMIN_CONFIG") or "config.yaml"
    db = args.db or source.get("POCKETROLE_ADMIN_DB") or "db/pocketrole.db"
    archive_root = args.archive_root or source.get("POCKETROLE_ADMIN_ARCHIVE_ROOT") or "web/published"
    admin_user = args.admin_user or source.get("POCKETROLE_ADMIN_USER") or "admin"
    admin_password = args.admin_password or source.get("POCKETROLE_ADMIN_PASSWORD") or "admin"
    cookie_secure = (
        args.cookie_secure
        if args.cookie_secure is not None
        else _env_bool(source, "POCKETROLE_ADMIN_COOKIE_SECURE", True)
    )
    if (admin_user, admin_password) == ("admin", "admin"):
        raise ValueError("Hosted admin credentials must be explicitly set and cannot use admin/admin.")
    return HostedAdminSettings(
        host=host,
        port=port,
        config=config,
        db=db,
        stories=args.stories,
        archive_root=archive_root,
        admin_user=admin_user,
        admin_password=admin_password,
        cookie_secure=cookie_secure,
    )


async def _publish_loop(app: web.Application) -> None:
    db = app[DB_KEY]
    builder = app[ARCHIVE_BUILDER_KEY]
    publisher_state = app[PUBLISHER_STATE_KEY]
    while True:
        interval = int(await db.get_system_setting("archive_publish_interval_minutes", default="5") or "5")
        publisher_state["status"] = "running"
        publisher_state["last_error"] = None
        try:
            for publication in await db.list_story_publications():
                if publication["visibility"] != "public":
                    continue
                await builder.build_story_archive(publication["story_id"])
            await builder.build_catalog()
        except Exception as exc:
            publisher_state["status"] = "error"
            publisher_state["last_error"] = str(exc)
        else:
            publisher_state["status"] = "idle"
        publisher_state["last_run_at"] = datetime.now(UTC).isoformat()
        await asyncio.sleep(max(interval, 1) * 60)


async def start_admin_service(args: argparse.Namespace | HostedAdminSettings) -> web.Application:
    settings = args if isinstance(args, HostedAdminSettings) else resolve_settings(args)
    config = load_config(settings.config)
    setup_logging(config.logging)
    db = DatabaseManager(settings.db, migrations_dir=MIGRATIONS_DIR)
    await db.initialize()
    await db.ensure_admin_user(settings.admin_user, settings.admin_password)

    stories = settings.stories
    if not stories:
        stories = [row["id"] for row in await db.get_stories()]

    for story_id in stories:
        story = await db.get_story(story_id)
        if story is None:
            raise ValueError(f"Story not found: {story_id}")

    resolved_llm_configs = ensure_story_llm_runtime_requirements(config, stories)
    required_providers = required_story_llm_providers(config, stories)

    router = LLMRouter(config.llm, required_providers=required_providers)
    await router.start()
    runtime = HostedRuntimeController(
        story_ids=stories,
        db=db,
        router=router,
        config=config,
        resolved_llm_configs=resolved_llm_configs,
    )
    await runtime.initialize()

    app = await create_app(
        db=db,
        runtime_controller=runtime,
        archive_root=settings.archive_root,
        admin_base_url="/admin",
        session_cookie_secure=settings.cookie_secure,
        news_mode_config=config.news_mode,
        story_web_post_targets=config.web_post_targets,
    )
    publish_task = asyncio.create_task(_publish_loop(app), name="archive-publish-loop")

    async def on_cleanup(_: web.Application) -> None:
        publish_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await publish_task
        for story_id, state in runtime.list_states().items():
            if state == "running":
                await runtime.stop_story(story_id)
        await router.stop()
        await db.close()

    app.on_cleanup.append(on_cleanup)
    return app


def main(argv: list[str] | None = None) -> None:
    settings = resolve_settings(parse_args(argv))

    async def runner() -> None:
        app = await start_admin_service(settings)
        runner_ = web.AppRunner(app)
        await runner_.setup()
        site = web.TCPSite(runner_, host=settings.host, port=settings.port)
        await site.start()
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            await runner_.cleanup()

    asyncio.run(runner())


if __name__ == "__main__":
    main()

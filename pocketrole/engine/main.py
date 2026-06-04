"""engine/main.py — エントリーポイント。

設定読み込み → ロギング設定 → DB/LLM初期化 → ヘルスチェック →
WebPoster 起動 → ProcessManager でマルチストーリー並行起動 → シグナル停止。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import sys
from datetime import UTC, datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

from db.db_manager import DatabaseManager
from engine.config import (
    Config,
    LoggingConfig,
    load_config,
)
from engine.llm.router import LLMRouter
from engine.llm_runtime import (
    ensure_story_llm_runtime_requirements,
    required_story_llm_providers,
)
from engine.process_manager import ProcessManager
from engine.story_engine import StoryEngine
from engine.web_post_targets import resolve_story_web_poster_config_with_db
from engine.web_poster import WebPoster

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent.parent / "db" / "migrations"


# ============================================================
# JSON ログフォーマッター
# ============================================================


class JsonFormatter(logging.Formatter):
    """ログレコードを JSON 形式に変換するフォーマッター。"""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "module": record.module,
            "msg": record.getMessage(),
        }
        for key in (
            "story_id",
            "char_id",
            "turn",
            "llm_provider",
            "llm_model",
            "llm_request_tag",
            "llm_latency_ms",
            "llm_system_prompt_chars",
            "llm_user_prompt_chars",
            "llm_response_chars",
            "llm_thinking_chars",
            "llm_done_reason",
            "llm_completion_status",
            "llm_recovery_attempted",
            "structured_policy_name",
            "llm_response_keys",
            "llm_message_keys",
            "llm_raw_preview",
        ):
            val = getattr(record, key, None)
            if val is not None:
                log_entry[key] = val
        if record.exc_info:
            log_entry["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            log_entry["stack_info"] = record.stack_info
        return json.dumps(log_entry, ensure_ascii=False)


# ============================================================
# ロギング設定
# ============================================================


def setup_logging(
    config: LoggingConfig,
    log_level_override: str | None = None,
) -> None:
    """ルートロガーを設定する。

    Args:
        config:             LoggingConfig（level / log_dir / retention_days を使用）
        log_level_override: CLI の --log-level 指定。config.level より優先される。
    """
    level_str = (log_level_override or config.level).upper()
    level = getattr(logging, level_str, logging.INFO)

    log_dir = Path(config.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    # 既存ハンドラーをすべて削除（多重登録防止）
    root_logger.handlers.clear()

    formatter = JsonFormatter()

    # stderr へのストリームハンドラー
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    # 日次ローテーションファイルハンドラー
    file_handler = TimedRotatingFileHandler(
        log_dir / "engine.log",
        when="midnight",
        backupCount=config.retention_days,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)


# ============================================================
# CLI 引数パーサー
# ============================================================


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """コマンドライン引数を解析して Namespace を返す。

    Args:
        argv: 引数リスト（None なら sys.argv[1:]）

    Returns:
        argparse.Namespace（stories / config / db / log_level）
    """
    parser = argparse.ArgumentParser(
        prog="pocketrole-engine",
        description="PocketRole シミュレーションエンジン",
    )
    parser.add_argument(
        "--stories",
        nargs="+",
        required=True,
        help="実行するストーリーID（複数指定可）",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="設定ファイルパス（デフォルト: config.yaml）",
    )
    parser.add_argument(
        "--db",
        default="db/pocketrole.db",
        help="SQLite データベースパス（デフォルト: db/pocketrole.db）",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        dest="log_level",
        help="ログレベルの上書き（DEBUG/INFO/WARNING/ERROR）",
    )
    return parser.parse_args(argv)


# ============================================================
# 非同期メイン処理
# ============================================================


async def async_main(config: Config, args: argparse.Namespace) -> None:
    """DB・LLM の初期化からエンジン実行までを行う非同期エントリーポイント。

    Args:
        config: 読み込み済みの Config
        args:   parse_args() の結果
    """
    logger.info(
        "startup stage: open DatabaseManager db_path=%s stories=%s",
        args.db,
        args.stories,
    )
    async with DatabaseManager(args.db, migrations_dir=MIGRATIONS_DIR) as db:
        logger.info(
            "startup stage complete: DatabaseManager ready db_path=%s stories=%s",
            args.db,
            args.stories,
        )
        for story_id in args.stories:
            story = await db.get_story(story_id)
            if story is None:
                raise ValueError(f"Story not found: {story_id}")

        resolved_llm_configs = ensure_story_llm_runtime_requirements(config, args.stories)
        required_providers = required_story_llm_providers(config, args.stories)
        for story_id, runtime_cfg in resolved_llm_configs.items():
            logger.info(
                "Resolved runtime LLM config",
                extra={
                    "story_id": story_id,
                    "llm_provider": runtime_cfg.provider,
                    "llm_model": runtime_cfg.model,
                },
            )

        router = LLMRouter(config.llm, required_providers=required_providers)
        await router.start()
        logger.info(
            "startup stage complete: LLMRouter started stories=%s",
            args.stories,
        )
        news_store: Any | None = None
        news_fetcher: Any | None = None
        news_fetcher_task: asyncio.Task[None] | None = None

        async def _stop_news_fetcher() -> None:
            nonlocal news_store, news_fetcher, news_fetcher_task
            if news_fetcher is not None:
                await news_fetcher.stop()
            if news_fetcher_task is not None and not news_fetcher_task.done():
                news_fetcher_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await news_fetcher_task
            news_fetcher_task = None
            news_fetcher = None
            if news_store is not None:
                await news_store.close()
                news_store = None

        try:
            # ヘルスチェック（失敗しても続行。結果はログに出力）
            results = await router.health_check_all()
            for provider, ok in results.items():
                if ok:
                    logger.info("LLM health check OK", extra={"llm_provider": provider})
                else:
                    logger.warning("LLM health check FAILED", extra={"llm_provider": provider})
            logger.info(
                "startup stage complete: LLM health checks finished stories=%s",
                args.stories,
            )

            # 時事モード: NewsArticleStore 初期化 + NewsFetcher 常駐起動
            if config.news_mode.enabled:
                try:
                    from db.news_store import NewsArticleStore
                    from engine.news_fetcher import NewsFetcher
                    news_store = NewsArticleStore()
                    await news_store.initialize()
                    news_fetcher = NewsFetcher(news_store, config.news_mode)
                    news_fetcher_task = asyncio.create_task(
                        news_fetcher.run(),
                        name="news-fetcher",
                    )
                    logger.info("startup stage complete: NewsFetcher started")
                except Exception as exc:
                    if news_store is not None:
                        await news_store.close()
                        news_store = None
                    logger.warning("NewsFetcher startup failed (時事モード無効で続行): %s", exc)

            # WebPoster 起動（per-story）
            web_posters: dict[str, WebPoster] = {}
            poster_tasks: list[asyncio.Task[None]] = []
            for story_id in args.stories:
                poster_config = await resolve_story_web_poster_config_with_db(
                    config,
                    story_id,
                    db,
                )
                if poster_config is None:
                    continue
                wp = WebPoster(story_id, poster_config, db)
                await wp.start()
                web_posters[story_id] = wp
                poster_tasks.append(
                    asyncio.create_task(wp.run(), name=f"poster-{story_id}")
                )
            logger.info(
                "startup stage complete: WebPosters started stories=%s",
                sorted(web_posters.keys()),
            )

            # ProcessManager（StoryEngine に web_poster を DI）
            def engine_factory(
                sid: str, _db: DatabaseManager, _router: LLMRouter
            ) -> StoryEngine:
                return StoryEngine(
                    sid, _db, _router,
                    web_poster=web_posters.get(sid),
                    config=config,
                    resolved_llm_config=resolved_llm_configs.get(sid),
                    news_store=news_store,
                )

            pm = ProcessManager(
                story_ids=args.stories,
                db=db,
                router=router,
                engine_factory=engine_factory,
            )

            # グレースフルシャットダウン
            async def _shutdown() -> None:
                await pm.stop_all()
                for wp in web_posters.values():
                    await wp.stop()
                await _stop_news_fetcher()

            loop = asyncio.get_running_loop()
            try:
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.add_signal_handler(
                        sig, lambda: asyncio.ensure_future(_shutdown())
                    )
            except NotImplementedError:
                logger.warning("Signal handlers not supported on this platform")

            await pm.start_all()
            logger.info(
                "startup stage complete: ProcessManager started stories=%s",
                args.stories,
            )
            # 全エンジンタスクが完了するまで待機
            await pm.wait_all()
        finally:
            await _stop_news_fetcher()
            await router.stop()

# ============================================================
# 同期エントリーポイント
# ============================================================


def main() -> int:
    """CLI エントリーポイント。終了コードを返す。

    Returns:
        0: 正常終了、1: エラー終了
    """
    args = parse_args()

    try:
        config = load_config(args.config)
    except Exception as e:
        print(f"ERROR: 設定読み込み失敗: {e}", file=sys.stderr)
        return 1

    setup_logging(config.logging, args.log_level)
    logger.info(
        "PocketRole engine starting: db_path=%s stories=%s",
        args.db,
        args.stories,
        extra={"story_id": str(args.stories)},
    )

    exit_code = 0
    try:
        asyncio.run(async_main(config, args))
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.exception("Unexpected error: %s", e)
        exit_code = 1

    logger.info(
        "PocketRole engine stopped: db_path=%s stories=%s",
        args.db,
        args.stories,
        extra={"story_id": str(args.stories)},
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

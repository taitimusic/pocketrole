"""engine/main.py のテスト。

parse_args / setup_logging / async_main の各グループをカバーする（14件）。
LLM・DB 依存部分はモック化。
"""

from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from engine.config import (
    CloudProviderConfig,
    LLMConfig,
    LLMRuntimeConfig,
    LoggingConfig,
    OllamaProviderConfig,
)
from engine.main import async_main, parse_args, setup_logging


# ============================================================
# ヘルパー
# ============================================================


def make_logging_config(tmp_path) -> LoggingConfig:
    """テスト用 LoggingConfig を生成する。"""
    return LoggingConfig(
        level="DEBUG",
        log_dir=str(tmp_path / "logs"),
        rotation="daily",
        retention_days=7,
    )


def make_mock_db():
    """DatabaseManager の非同期コンテキストマネージャーモックを返す。"""
    db = AsyncMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=False)
    db.get_story = AsyncMock(
        return_value={
            "id": "test_story",
            "llm_provider": "ollama",
            "llm_model": "qwen2.5:14b",
            "web_receiver_url": None,
            "web_auth_token": None,
        }
    )
    db.get_system_setting = AsyncMock(return_value=None)
    return db


def make_mock_config():
    """LLMConfig と LoggingConfig を持つ Config モックを返す。"""
    config = MagicMock()
    config.llm = LLMConfig(
        cloud_concurrency=3,
        providers={
            "ollama": OllamaProviderConfig(
                base_url="http://localhost:11434",
                default_model="qwen2.5:14b",
                timeout_sec=120,
                max_retries=3,
            ),
            "openai": CloudProviderConfig(
                default_model="gpt-5.4-nano",
                timeout_sec=60,
                max_retries=3,
                api_key="test-openai-key",
                api_mode="auto",
            ),
        },
    )
    config.llm_runtime = LLMRuntimeConfig()
    config.llm_runtime.active_profile = "ollama_local"
    config.llm_runtime.profiles = {
        "ollama_local": MagicMock(provider="ollama", model="qwen2.5:14b"),
    }
    config.web_poster.batch_size = 10
    config.web_poster.retry_interval_sec = 30
    config.web_poster.max_consecutive_failures = 3
    config.web_poster.pause_duration_sec = 300
    config.news_mode.enabled = False
    config.web_post_targets = {
        "test_story": MagicMock(enabled=True, receiver_url="https://example.com/receiver.php", auth_token="tok"),
        "story_a": MagicMock(enabled=True, receiver_url="https://a.example.com/receiver.php", auth_token="tok-a"),
        "story_b": MagicMock(enabled=True, receiver_url="https://b.example.com/receiver.php", auth_token="tok-b"),
        "disabled_story": MagicMock(enabled=False, receiver_url="", auth_token=""),
    }
    return config


def make_args(stories: list[str] | None = None, db: str = ":memory:") -> MagicMock:
    """async_main 用の args モックを返す。"""
    args = MagicMock()
    args.stories = stories if stories is not None else ["test_story"]
    args.db = db
    return args


# ============================================================
# parse_args テスト
# ============================================================


class TestParseArgs:
    def test_parse_args_story_required(self):
        """--stories がないと SystemExit が発生する。"""
        with pytest.raises(SystemExit):
            parse_args([])

    def test_parse_args_defaults(self):
        """--config / --db / --log-level のデフォルト値が正しい。"""
        ns = parse_args(["--stories", "ankoku_gakuen"])
        assert ns.config == "config.yaml"
        assert ns.db == "db/pocketrole.db"
        assert ns.log_level is None

    def test_parse_args_story_value(self):
        """--stories の値が Namespace に正しく格納される。"""
        ns = parse_args(["--stories", "ankoku_gakuen"])
        assert ns.stories == ["ankoku_gakuen"]

    def test_parse_args_all_options(self):
        """全オプションを指定できる。"""
        ns = parse_args([
            "--stories", "my_story", "another_story",
            "--config", "custom.yaml",
            "--db", "data/test.db",
            "--log-level", "DEBUG",
        ])
        assert ns.stories == ["my_story", "another_story"]
        assert ns.config == "custom.yaml"
        assert ns.db == "data/test.db"
        assert ns.log_level == "DEBUG"


# ============================================================
# setup_logging テスト
# ============================================================


class TestSetupLogging:
    def _restore_root_logger(self, original_level, original_handlers):
        """ルートロガーをテスト前の状態に戻す。"""
        root = logging.getLogger()
        root.setLevel(original_level)
        root.handlers.clear()
        root.handlers.extend(original_handlers)

    @pytest.fixture(autouse=True)
    def save_restore_root_logger(self):
        """テスト前後でルートロガーを保存・復元する。"""
        root = logging.getLogger()
        original_level = root.level
        original_handlers = list(root.handlers)
        yield
        self._restore_root_logger(original_level, original_handlers)

    def test_setup_logging_creates_log_dir(self, tmp_path):
        """log_dir が存在しなくても自動作成される。"""
        cfg = make_logging_config(tmp_path)
        log_dir = tmp_path / "logs"
        assert not log_dir.exists()
        setup_logging(cfg)
        assert log_dir.exists()

    def test_setup_logging_sets_level(self, tmp_path):
        """ルートロガーのレベルが LoggingConfig.level になる。"""
        cfg = make_logging_config(tmp_path)
        cfg = LoggingConfig(
            level="WARNING",
            log_dir=str(tmp_path / "logs"),
            rotation="daily",
            retention_days=7,
        )
        setup_logging(cfg)
        assert logging.getLogger().level == logging.WARNING

    def test_setup_logging_has_stream_handler(self, tmp_path):
        """StreamHandler がルートロガーに追加される。"""
        cfg = make_logging_config(tmp_path)
        setup_logging(cfg)
        handler_types = [type(h) for h in logging.getLogger().handlers]
        assert logging.StreamHandler in handler_types

    def test_setup_logging_has_file_handler(self, tmp_path):
        """TimedRotatingFileHandler がルートロガーに追加される。"""
        cfg = make_logging_config(tmp_path)
        setup_logging(cfg)
        handler_types = [type(h) for h in logging.getLogger().handlers]
        assert TimedRotatingFileHandler in handler_types

    def test_setup_logging_level_override(self, tmp_path):
        """log_level_override が config.level より優先される。"""
        cfg = LoggingConfig(
            level="WARNING",
            log_dir=str(tmp_path / "logs"),
            rotation="daily",
            retention_days=7,
        )
        setup_logging(cfg, log_level_override="ERROR")
        assert logging.getLogger().level == logging.ERROR


# ============================================================
# async_main テスト
# ============================================================


def make_mock_pm(tasks: dict | None = None):
    """ProcessManager モックを返す。"""
    pm = AsyncMock()
    pm.start_all = AsyncMock()
    pm.stop_all = AsyncMock()
    pm.wait_all = AsyncMock()
    pm._tasks = tasks or {}
    return pm


def make_mock_web_poster():
    """WebPoster モックを返す。"""
    wp = AsyncMock()
    wp.start = AsyncMock()
    wp.run = AsyncMock()
    wp.stop = AsyncMock()
    return wp


class TestAsyncMain:
    @pytest.fixture
    def mock_db(self):
        return make_mock_db()

    @pytest.fixture
    def mock_router(self):
        router = AsyncMock()
        router.start = AsyncMock()
        router.stop = AsyncMock()
        router.health_check_all = AsyncMock(return_value={"ollama": True})
        return router

    @pytest.fixture
    def mock_pm(self):
        return make_mock_pm()

    @pytest.fixture
    def mock_wp(self):
        return make_mock_web_poster()

    async def test_async_main_initializes_db(self, mock_db, mock_router, mock_pm, mock_wp):
        """DatabaseManager.__aenter__ が呼ばれる（DB 初期化）。"""
        config = make_mock_config()
        args = make_args()

        with (
            patch("engine.main.DatabaseManager", return_value=mock_db),
            patch("engine.main.LLMRouter", return_value=mock_router),
            patch("engine.main.ProcessManager", return_value=mock_pm),
            patch("engine.main.WebPoster", return_value=mock_wp),
        ):
            await async_main(config, args)

        mock_db.__aenter__.assert_called_once()

    async def test_async_main_starts_router(self, mock_db, mock_router, mock_pm, mock_wp):
        """LLMRouter.start() が呼ばれる。"""
        config = make_mock_config()
        args = make_args()

        with (
            patch("engine.main.DatabaseManager", return_value=mock_db),
            patch("engine.main.LLMRouter", return_value=mock_router),
            patch("engine.main.ProcessManager", return_value=mock_pm),
            patch("engine.main.WebPoster", return_value=mock_wp),
        ):
            await async_main(config, args)

        mock_router.start.assert_called_once()

    async def test_async_main_health_check(self, mock_db, mock_router, mock_pm, mock_wp):
        """router.health_check_all() が呼ばれる。"""
        config = make_mock_config()
        args = make_args()

        with (
            patch("engine.main.DatabaseManager", return_value=mock_db),
            patch("engine.main.LLMRouter", return_value=mock_router),
            patch("engine.main.ProcessManager", return_value=mock_pm),
            patch("engine.main.WebPoster", return_value=mock_wp),
        ):
            await async_main(config, args)

        mock_router.health_check_all.assert_called_once()

    async def test_async_main_starts_process_manager(self, mock_db, mock_router, mock_pm, mock_wp):
        """ProcessManager.start_all() が呼ばれる。"""
        config = make_mock_config()
        args = make_args()

        with (
            patch("engine.main.DatabaseManager", return_value=mock_db),
            patch("engine.main.LLMRouter", return_value=mock_router),
            patch("engine.main.ProcessManager", return_value=mock_pm),
            patch("engine.main.WebPoster", return_value=mock_wp),
        ):
            await async_main(config, args)

        mock_pm.start_all.assert_called_once()
        mock_pm.wait_all.assert_awaited_once()

    async def test_async_main_router_stop_on_error(self, mock_db, mock_router, mock_wp):
        """ProcessManager.start_all() が例外を出しても router.stop() が finally で呼ばれる。"""
        config = make_mock_config()
        args = make_args()

        broken_pm = make_mock_pm()
        broken_pm.start_all = AsyncMock(side_effect=RuntimeError("pm error"))

        with (
            patch("engine.main.DatabaseManager", return_value=mock_db),
            patch("engine.main.LLMRouter", return_value=mock_router),
            patch("engine.main.ProcessManager", return_value=broken_pm),
            patch("engine.main.WebPoster", return_value=mock_wp),
            pytest.raises(RuntimeError, match="pm error"),
        ):
            await async_main(config, args)

        mock_router.stop.assert_called_once()

    async def test_async_main_propagates_story_task_crash(self, mock_db, mock_router, mock_wp):
        """通常実行中の story task クラッシュは async_main から呼び出し元へ返す。"""
        config = make_mock_config()
        args = make_args()

        broken_pm = make_mock_pm()
        broken_pm.wait_all = AsyncMock(side_effect=RuntimeError("story task crashed"))

        with (
            patch("engine.main.DatabaseManager", return_value=mock_db),
            patch("engine.main.LLMRouter", return_value=mock_router),
            patch("engine.main.ProcessManager", return_value=broken_pm),
            patch("engine.main.WebPoster", return_value=mock_wp),
            pytest.raises(RuntimeError, match="story task crashed"),
        ):
            await async_main(config, args)

        broken_pm.stop_all.assert_not_called()
        mock_router.stop.assert_called_once()

    async def test_async_main_logs_startup_stages(
        self, caplog, mock_db, mock_router, mock_pm, mock_wp
    ):
        """startup stage の境界ログが順に出る。"""
        config = make_mock_config()
        args = make_args(stories=["test_story"], db="db/test.db")

        with (
            patch("engine.main.DatabaseManager", return_value=mock_db),
            patch("engine.main.LLMRouter", return_value=mock_router),
            patch("engine.main.ProcessManager", return_value=mock_pm),
            patch("engine.main.WebPoster", return_value=mock_wp),
            caplog.at_level(logging.INFO, logger="engine.main"),
        ):
            await async_main(config, args)

        messages = [record.message for record in caplog.records if record.name == "engine.main"]
        assert any("startup stage: open DatabaseManager" in message for message in messages)
        assert any("startup stage complete: DatabaseManager ready" in message for message in messages)
        assert any("startup stage complete: LLMRouter started" in message for message in messages)
        assert any("startup stage complete: LLM health checks finished" in message for message in messages)
        assert any("startup stage complete: WebPosters started" in message for message in messages)
        assert any("startup stage complete: ProcessManager started" in message for message in messages)

    async def test_async_main_skips_web_poster_when_story_target_missing(self, mock_router, mock_pm, mock_wp):
        """投稿設定が無い story は WebPoster なしで起動する。"""
        config = make_mock_config()
        args = make_args(stories=["missing_story"])
        mock_db = make_mock_db()

        with (
            patch("engine.main.DatabaseManager", return_value=mock_db) as mock_db_cls,
            patch("engine.main.LLMRouter", return_value=mock_router),
            patch("engine.main.ProcessManager", return_value=mock_pm),
            patch("engine.main.WebPoster", return_value=mock_wp) as poster_cls,
        ):
            await async_main(config, args)

        mock_db_cls.assert_called_once()
        poster_cls.assert_not_called()

    async def test_async_main_skips_disabled_story(self, mock_db, mock_router, mock_pm, mock_wp):
        """enabled=false の story では WebPoster を作らない。"""
        config = make_mock_config()
        args = make_args(stories=["disabled_story"])

        with (
            patch("engine.main.DatabaseManager", return_value=mock_db),
            patch("engine.main.LLMRouter", return_value=mock_router),
            patch("engine.main.ProcessManager", return_value=mock_pm),
            patch("engine.main.WebPoster", return_value=mock_wp) as poster_cls,
        ):
            await async_main(config, args)

        poster_cls.assert_not_called()

    async def test_async_main_uses_story_specific_targets(self, mock_db, mock_router, mock_pm):
        """story ごとに別 receiver/auth を持つ WebPosterConfig を渡す。"""
        config = make_mock_config()
        args = make_args(stories=["story_a", "story_b"])
        mock_wp_a = make_mock_web_poster()
        mock_wp_b = make_mock_web_poster()

        with (
            patch("engine.main.DatabaseManager", return_value=mock_db),
            patch("engine.main.LLMRouter", return_value=mock_router),
            patch("engine.main.ProcessManager", return_value=mock_pm),
            patch("engine.main.WebPoster", side_effect=[mock_wp_a, mock_wp_b]) as poster_cls,
        ):
            await async_main(config, args)

        first_cfg = poster_cls.call_args_list[0].args[1]
        second_cfg = poster_cls.call_args_list[1].args[1]
        assert first_cfg.receiver_url == "https://a.example.com/receiver.php"
        assert first_cfg.auth_token == "tok-a"
        assert second_cfg.receiver_url == "https://b.example.com/receiver.php"
        assert second_cfg.auth_token == "tok-b"

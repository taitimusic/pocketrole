"""tools/smoke_engine_start.py のテスト。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.smoke_engine_start import parse_args, run_smoke


def test_parse_args_defaults() -> None:
    """--db と --turns のデフォルト値が正しい。"""
    ns = parse_args(["--story", "ankoku_gakuen"])
    assert ns.story == "ankoku_gakuen"
    assert ns.db == "db/pocketrole.db"
    assert ns.turns == 1


@pytest.mark.asyncio
async def test_run_smoke_copies_db_and_returns_summary(tmp_path: Path) -> None:
    """run_smoke() は DB を /tmp 側へコピーして指定ターン数ぶん実行する。"""
    source_db = tmp_path / "source.db"
    source_db.write_text("seed", encoding="utf-8")

    config = MagicMock()
    config.llm = MagicMock()

    fake_db = AsyncMock()
    fake_db.__aenter__ = AsyncMock(return_value=fake_db)
    fake_db.__aexit__ = AsyncMock(return_value=False)

    count_cursor = AsyncMock()
    count_cursor.fetchone = AsyncMock(return_value=(1,))

    last_cursor = AsyncMock()
    last_cursor.fetchone = AsyncMock(return_value=("char_a", 0, 65))

    fake_db._conn = AsyncMock()
    fake_db._conn.execute = AsyncMock(side_effect=[count_cursor, last_cursor])

    fake_router = AsyncMock()
    fake_router.start = AsyncMock()
    fake_router.stop = AsyncMock()

    fake_engine = AsyncMock()
    fake_engine.initialize = AsyncMock()
    fake_engine.run_one_turn = AsyncMock()

    with (
        patch("tools.smoke_engine_start.load_config", return_value=config),
        patch("tools.smoke_engine_start.DatabaseManager", return_value=fake_db) as db_cls,
        patch(
            "tools.smoke_engine_start.ensure_story_llm_runtime_requirements",
            return_value={"ankoku_gakuen_mystery": MagicMock()},
        ),
        patch(
            "tools.smoke_engine_start.required_story_llm_providers",
            return_value=set(),
        ),
        patch("tools.smoke_engine_start.LLMRouter", return_value=fake_router),
        patch("tools.smoke_engine_start.StoryEngine", return_value=fake_engine) as engine_cls,
    ):
        result = await run_smoke("ankoku_gakuen_mystery", source_db, turns=2)

    work_db_path = Path(db_cls.call_args.args[0])
    assert work_db_path != source_db
    assert work_db_path.exists()
    assert work_db_path.read_text(encoding="utf-8") == "seed"
    assert result["work_db_path"] == str(work_db_path)
    assert result["chat_log_count"] == 1
    assert result["last_turn_number"] == 0
    assert result["last_message_len"] == 65
    assert fake_engine.initialize.await_count == 1
    assert fake_engine.run_one_turn.await_count == 2
    fake_router.start.assert_awaited_once()
    fake_router.stop.assert_awaited_once()
    assert engine_cls.call_args.kwargs["config"] is config

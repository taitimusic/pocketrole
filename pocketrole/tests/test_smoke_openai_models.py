"""tools/smoke_openai_models.py のテスト。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from tools.smoke_openai_models import parse_args, run_openai_model_smokes


def test_parse_args_defaults() -> None:
    ns = parse_args(["--story", "ankoku_gakuen"])
    assert ns.story == "ankoku_gakuen"
    assert ns.turns == 1
    assert ns.models == []


@pytest.mark.asyncio
async def test_run_openai_model_smokes_filters_visible_models_and_collects_results(
    tmp_path: Path,
) -> None:
    source_db = tmp_path / "source.db"
    source_db.write_text("seed", encoding="utf-8")

    with (
        patch(
            "tools.smoke_openai_models._list_visible_openai_models",
            new=AsyncMock(return_value=["gpt-5-mini", "gpt-4o-mini"]),
        ),
        patch(
            "tools.smoke_openai_models._copy_source_db",
            side_effect=[
                tmp_path / "gpt-5-mini.db",
                tmp_path / "gpt-4o-mini.db",
            ],
        ),
        patch(
            "tools.smoke_openai_models._write_temp_openai_config",
            side_effect=[
                tmp_path / "gpt-5-mini.yaml",
                tmp_path / "gpt-4o-mini.yaml",
            ],
        ),
        patch(
            "tools.smoke_openai_models.run_smoke",
            new=AsyncMock(
                side_effect=[
                    {
                        "story_id": "ankoku_gakuen",
                        "chat_log_count": 3,
                        "last_char_id": "runa",
                        "last_message_len": 42,
                        "work_db_path": "/tmp/work1.db",
                    },
                    RuntimeError("Authentication failed"),
                ]
            ),
        ),
    ):
        result = await run_openai_model_smokes(
            "ankoku_gakuen",
            db_path=source_db,
            models=["gpt-5-mini", "gpt-5.4-mini", "gpt-4o-mini"],
            turns=1,
            config_path="config.yaml",
            env_path=".env",
        )

    assert result["visible_models"] == ["gpt-5-mini", "gpt-4o-mini"]
    assert result["requested_models"] == ["gpt-5-mini", "gpt-5.4-mini", "gpt-4o-mini"]
    assert result["candidate_models"] == ["gpt-5-mini", "gpt-4o-mini"]
    assert result["skipped_models"] == ["gpt-5.4-mini"]
    assert result["results"][0]["model"] == "gpt-5-mini"
    assert result["results"][0]["success"] is True
    assert result["results"][1]["model"] == "gpt-4o-mini"
    assert result["results"][1]["success"] is False
    assert result["results"][1]["error_type"] == "RuntimeError"

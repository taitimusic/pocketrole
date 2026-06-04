#!/usr/bin/env python3
"""OpenAI 利用可能モデルを比較 smoke する CLI。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

import yaml

from engine.config import CloudProviderConfig, load_config
from engine.llm.openai_client import OpenAIClient
from tools.smoke_engine_start import run_smoke

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="OpenAI の複数モデルを /tmp の DB コピーで比較 smoke する"
    )
    parser.add_argument("--story", required=True, help="対象ストーリーID")
    parser.add_argument(
        "--db",
        default="db/pocketrole.db",
        help="SQLite DB ファイルパス（デフォルト: db/pocketrole.db）",
    )
    parser.add_argument(
        "--turns",
        type=int,
        default=1,
        help="各モデルで実行するターン数（デフォルト: 1）",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="設定ファイルパス（デフォルト: config.yaml）",
    )
    parser.add_argument(
        "--env",
        default=".env",
        help=".env パス（デフォルト: .env）",
    )
    parser.add_argument(
        "--models",
        nargs="*",
        default=[],
        help="試す OpenAI モデル一覧。未指定時は標準候補を使用する",
    )
    return parser.parse_args(argv)


def _copy_source_db(db_path: str | Path, story_id: str, model: str) -> Path:
    source = Path(db_path)
    suffix = f"{story_id}-{model.replace('/', '_')}-{uuid.uuid4().hex}.db"
    work_db = Path(tempfile.gettempdir()) / f"pocketrole-openai-smoke-{suffix}"
    shutil.copy2(source, work_db)
    return work_db


def _write_temp_openai_config(config_path: str | Path, model: str) -> Path:
    source = Path(config_path)
    with source.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    llm_cfg = data.setdefault("llm", {})
    providers = llm_cfg.setdefault("providers", {})

    openai_cfg = providers.setdefault("openai", {})
    openai_cfg["default_model"] = model
    openai_cfg["api_mode"] = "auto"

    temp_path = Path(tempfile.gettempdir()) / (
        f"pocketrole-openai-smoke-{model.replace('/', '_')}-{uuid.uuid4().hex}.yaml"
    )
    with temp_path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)
    return temp_path


def _write_temp_runtime_profile(model: str) -> Path:
    data = {
        "active_profile": "candidate",
        "profiles": {
            "candidate": {
                "provider": "openai",
                "model": model,
            }
        },
        "story_overrides": {},
    }
    temp_path = Path(tempfile.gettempdir()) / (
        f"pocketrole-openai-runtime-{model.replace('/', '_')}-{uuid.uuid4().hex}.yaml"
    )
    with temp_path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)
    return temp_path


async def _list_visible_openai_models(
    config_path: str = "config.yaml",
    env_path: str = ".env",
) -> list[str]:
    temp_config = _write_temp_openai_config(config_path, "gpt-5-mini")
    cfg = load_config(str(temp_config), env_path)
    provider_cfg = cfg.llm.providers.get("openai")
    if not isinstance(provider_cfg, CloudProviderConfig):
        return []

    client = OpenAIClient(
        api_key=provider_cfg.api_key,
        default_model=provider_cfg.default_model,
        timeout_sec=provider_cfg.timeout_sec,
        max_retries=provider_cfg.max_retries,
        api_mode=provider_cfg.api_mode or "auto",
    )
    models = await client.list_models()
    return sorted(models)


async def run_openai_model_smokes(
    story_id: str,
    db_path: str | Path = "db/pocketrole.db",
    *,
    models: list[str] | None = None,
    turns: int = 1,
    config_path: str = "config.yaml",
    env_path: str = ".env",
) -> dict[str, Any]:
    requested_models = list(models or ["gpt-5-mini", "gpt-5.4-mini", "gpt-4.1-mini", "gpt-4o-mini"])
    visible_models = await _list_visible_openai_models(config_path, env_path)
    visible_set = set(visible_models)
    candidate_models = [model for model in requested_models if model in visible_set]
    skipped_models = [model for model in requested_models if model not in visible_set]

    results: list[dict[str, Any]] = []
    for model in candidate_models:
        work_db = _copy_source_db(db_path, story_id, model)
        temp_config = _write_temp_openai_config(config_path, model)
        temp_runtime = _write_temp_runtime_profile(model)
        try:
            summary = await run_smoke(
                story_id,
                work_db,
                turns=turns,
                config_path=str(temp_config),
                env_path=env_path,
                llm_runtime_path=str(temp_runtime),
            )
        except Exception as exc:
            results.append(
                {
                    "model": model,
                    "success": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "config_path": str(temp_config),
                    "llm_runtime_path": str(temp_runtime),
                    "work_db_path": str(work_db),
                }
            )
            continue

        results.append(
            {
                "model": model,
                "success": True,
                "endpoint_mode": "responses" if model.lower().startswith("gpt-5") else "chat_completions",
                **summary,
                "config_path": str(temp_config),
                "llm_runtime_path": str(temp_runtime),
            }
        )

    return {
        "story_id": story_id,
        "turns_requested": turns,
        "visible_models": visible_models,
        "requested_models": requested_models,
        "candidate_models": candidate_models,
        "skipped_models": skipped_models,
        "results": results,
    }


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        summary = asyncio.run(
            run_openai_model_smokes(
                args.story,
                db_path=args.db,
                models=args.models,
                turns=args.turns,
                config_path=args.config,
                env_path=args.env,
            )
        )
    except Exception:
        logger.exception("openai model smoke failed")
        return EXIT_ERROR

    print(json.dumps(summary, ensure_ascii=False))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

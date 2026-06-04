# CLAUDE.md — PocketRole キャラ駆動ストーリー箱庭プラットフォーム

## Canonical Docs

このファイルが AI エージェント向けプロジェクトメモリの正本である。
作業時の参照順:

1. この `CLAUDE.md`
2. `docs/specs/README.md`
3. 関連する `docs/specs/*.md`
4. `docs/specs/known-gaps.md`
5. `docs/runbooks/*.md`
6. `AGENT_GUIDE.md`

`AGENTS.md` は Codex 互換メモとして残すが、正本はこのファイルに集約する。

## プロジェクト概要

LLM（大規模言語モデル）を使い、キャラクターが自律的に生活・会話・移動し、物語を自動生成する箱庭シミュレーションプラットフォーム。ローカルLLM（Ollama）にもクラウドLLM API（ChatGPT / Claude / Gemini / DeepSeek）にも対応し、設定で切り替え可能。生成されたチャットログはWebサイトにリアルタイム公開される。

## 重要ドキュメント

| ファイル | 内容 |
|---------|------|
| `docs/specs/README.md` | 実装仕様書群の索引（最初に読む） |
| `docs/specs/system-overview.md` | システム全体像 |
| `docs/specs/simulation-engine.md` | StoryEngine の current behavior |
| `docs/specs/runtime-architecture.md` | 起動から Web 投稿までのランタイム構造 |
| `docs/specs/data-model.md` | YAML / DB / JSONL のデータ構造 |
| `docs/specs/known-gaps.md` | 既知課題と未充足仕様 |
| `docs/specs/story-quality-playbook.md` | story 品質向上の実践ガイド |
| `docs/specs/web-surface.md` | 公開 viewer / API 仕様 |
| `docs/specs/hosted-admin-surface.md` | 管理 UI / admin API 仕様 |
| `docs/specs/story-lifecycle-and-tools.md` | story 運用フロー |

## 技術スタック

- **Python 3.11+**（asyncio ベース）
- **SQLite 3**（WALモード）
- **LLM**: Ollama（ローカル）/ OpenAI / Anthropic / Google Gemini / DeepSeek（クラウド）
- **PHP 8.x**（Webサーバー側）
- **vanilla JS + CSS**（フロントエンド）

## プロジェクト構造

```
pocketrole/
├── CLAUDE.md            # AI エージェント向けプロジェクトメモリ（正本）
├── AGENTS.md            # Codex 互換メモ
├── README.md            # プロジェクト概要（人間向け）
├── docs/
│   ├── specs/           # 実装仕様書群（現行実装の正本）
│   ├── runbooks/        # 運用手順書
│   ├── users_manual/    # ユーザーズガイド
│   └── ...
├── pocketrole/          # Python アプリケーション本体
│   ├── engine/          # シミュレーションエンジン
│   │   ├── llm/         # LLM プロバイダー（Strategy Pattern）
│   │   ├── story_engine.py
│   │   ├── story_director.py
│   │   ├── episode_planner.py
│   │   ├── growth_engine.py
│   │   ├── quality_guard.py
│   │   └── ...
│   ├── db/              # SQLite スキーマ・マイグレーション・DBManager
│   ├── admin/           # hosted admin surface（aiohttp）
│   ├── stories/         # ストーリー定義 YAML + キャラ画像
│   ├── tools/           # CLI ユーティリティ
│   ├── tests/           # pytest テスト
│   ├── web/             # PHP Web サーバー側
│   ├── config.yaml      # 全体設定
│   ├── .env             # API キー（Git 管理外）
│   └── requirements.txt # Python 依存
└── deploy/              # デプロイ構成（nginx, systemd）
```

## 開発コマンド

```bash
# テスト実行（全体）
cd pocketrole && .venv/bin/pytest tests/ -v --ignore=tests/test_integration.py

# テスト実行（個別）
.venv/bin/pytest tests/test_story_engine.py -v

# ストーリー YAML バリデーション
python -m tools.validate_story stories/ankoku_gakuen/

# ストーリーを DB にインポート
python -m tools.import_story stories/ankoku_gakuen/

# エンジン起動
python -m engine.main --stories ankoku_gakuen

# ログ確認
tail -f logs/engine.log | python -m json.tool
```

## コーディング規約

### Python

- Python 3.11+。全関数に型ヒント必須
- asyncio ベース。I/O 処理は全て `async/await`
- HTTP: `aiohttp`（requests は使わない）
- SQLite: `aiosqlite`（sqlite3 直接は使わない）
- フォーマッタ: black (line-length=100)、リンター: ruff
- ログ: `print()` 禁止。`logging` モジュール使用（構造化 JSON）
- DB: パラメータバインディング必須。文字列結合による SQL 構築禁止
- 設定値: ハードコード禁止。config.yaml か .env から取得
- API キー: コードに絶対に書かない。.env のみ

### 命名規則

- モジュール / 関数 / 変数: `snake_case`
- クラス: `PascalCase`
- 定数: `UPPER_SNAKE_CASE`
- キャラ ID / 場所 ID / ストーリー ID: `snake_case`
- LLM プロバイダー名: `ollama`, `openai`, `anthropic`, `gemini`, `deepseek`

### PHP（Web 側）

- PHP 8.0+。入力は必ずサニタイズ
- `json_encode` には `JSON_UNESCAPED_UNICODE` を常に付与

## テスト

- pytest + pytest-asyncio
- LLM 依存は `MockLLMClient`（`BaseLLMClient` 準拠）でモック化
- DB テストは SQLite in-memory（`:memory:`）
- テストファイル: `tests/test_{モジュール名}.py`
- フィクスチャは `tests/conftest.py` に集約

## LLM プロバイダーのアーキテクチャ

全 LLM アクセスは `LLMRouter` 経由。個別プロバイダーを直接呼ばない。

```
engine/llm/
├── base.py              BaseLLMClient（抽象基底クラス）+ LLMResponse
├── router.py            LLMRouter（振り分け + キュー/セマフォ管理）
├── exceptions.py        LLMError, LLMTimeoutError, LLMRateLimitError 等
├── ollama_client.py     Ollama（ローカル、直列処理）
├── openai_client.py     OpenAI ChatGPT（クラウド、並行可）
├── anthropic_client.py  Anthropic Claude（クラウド、並行可）
├── gemini_client.py     Google Gemini（クラウド、並行可）
└── deepseek_client.py   DeepSeek（OpenAIClient のサブクラス）
```

**キュー制御:**
- ローカル LLM（Ollama）→ `asyncio.Queue` で直列（VRAM 競合防止）
- クラウド API → `asyncio.Semaphore` で同時実行数制限（デフォルト 3）

## DB

- SQLite 3（WAL モード）
- 接続時に毎回 `PRAGMA journal_mode = WAL; PRAGMA foreign_keys = ON; PRAGMA busy_timeout = 5000;` を実行
- 全テーブルに `story_id` カラムあり（マルチストーリー対応）
- マイグレーションは `db/migrations/` に SQL 配置、`db_manager.py` 起動時に自動適用

## 現在の状態

- **バージョン:** v0.1.0-beta.1
- **状態:** 公開ベータ。API・設定形式が変わる可能性がある
- **主な導線:** `START_HERE.md` からサンプル story を起動し、Admin local viewer で確認する

## よくある注意点

- 感情値は 0.0〜1.0 の範囲。必ず `max(0.0, min(1.0, value))` でクランプする
- キャラの発言は 1〜3 文に制限（LLM プロンプトで指示）
- プロンプトはプロバイダー非依存。各クライアントが自プロバイダーの API 形式に変換する
- `chat_logs` に `llm_provider` / `llm_model` を記録する（トレーサビリティ）
- Web 送信は非同期バッチ。失敗してもローカル DB にログは残る
- story-emergence 層は opt-in feature flag 制御。`config.yaml` で有効化しないと旧経路で動く
- LLM の応答は不安定な場合がある。必ずリトライロジックを入れる（`engine/llm/exceptions.py` の例外を使う）
- クラウド API にはレート制限がある。`LLMRateLimitError` で `retry_after` を尊重する

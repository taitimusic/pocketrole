# AGENTS.md — PocketRole（Codex 互換メモ）

## Canonical Docs

正本は `CLAUDE.md` に移行した。Codex で作業する場合もまず `CLAUDE.md` を参照すること。

参照順:

1. `CLAUDE.md`
2. `docs/specs/README.md`
3. 関連する `docs/specs/*.md`
4. `docs/specs/known-gaps.md`
5. `docs/runbooks/*.md`
6. `AGENT_GUIDE.md`

## プロジェクト概要

LLMを使い、キャラクターが自律的に生活・会話・移動し、物語を自動生成する箱庭シミュレーション。ローカルLLM（Ollama）にもクラウドLLM API（ChatGPT / Claude / Gemini / DeepSeek）にも対応。Python 3.11+ の asyncio ベース。

## ドキュメント

| ファイル | 内容 |
|---------|------|
| `docs/specs/README.md` | 実装仕様書群の索引 |
| `docs/specs/system-overview.md` | システム全体像 |
| `docs/specs/simulation-engine.md` | StoryEngine の current behavior |
| `docs/specs/runtime-architecture.md` | ランタイム構造 |
| `docs/specs/data-model.md` | データ構造 |
| `docs/specs/known-gaps.md` | 既知課題 |
| `docs/runbooks/` | 運用手順 |
| `AGENT_GUIDE.md` | AI エージェント向け完全ガイド |

## 開発コマンド

```bash
cd pocketrole && .venv/bin/pytest tests/ -v --ignore=tests/test_integration.py
python -m tools.validate_story stories/ankoku_gakuen/
python -m engine.main --stories ankoku_gakuen
```

## コーディング規約

### Python

- Python 3.11+。全関数に型ヒント必須
- asyncio ベース。I/O処理は全て `async/await`
- HTTP: `aiohttp`、SQLite: `aiosqlite`
- フォーマッタ: black (line-length=100)、リンター: ruff
- ログ: `print()` 禁止。`logging` モジュール使用（構造化JSON）
- DB: パラメータバインディング必須。文字列結合によるSQL構築禁止
- 設定値: ハードコード禁止。config.yaml か .env から取得
- APIキー: コードに絶対に書かない

### 命名規則

- モジュール / 関数 / 変数: `snake_case`
- クラス: `PascalCase`
- 定数: `UPPER_SNAKE_CASE`

### PHP（Web側）

- PHP 8.0+。入力は必ずサニタイズ
- `json_encode` には `JSON_UNESCAPED_UNICODE` を常に付与

## テスト

- pytest + pytest-asyncio
- LLM依存は `MockLLMClient` でモック化
- DB テストは SQLite in-memory

## 現在の状態

- **バージョン:** v0.1.0-beta.1
- **状態:** 公開ベータ。API・設定形式が変わる可能性がある

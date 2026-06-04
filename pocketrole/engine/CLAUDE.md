# engine/ — シミュレーションエンジン

## モジュール境界

このディレクトリは PocketRole のシミュレーションエンジン本体を含む。
DB 操作は `db/db_manager.py` に委譲し、このディレクトリでは直接 SQL を書かない。

## Entry Point

- `main.py` → Config 読み込み → DB 初期化 → LLMRouter 起動 → ProcessManager → StoryEngine
- `story_engine.py` がメインループ。1 キャラ 1 ターン 17 ステップで生成を回す

## サブシステム

| ファイル | 責務 |
|---------|------|
| `story_engine.py` | メインループ、turn 生成、post-round 処理 |
| `story_director.py` | 緊張検出・介入生成（LLM + deterministic） |
| `episode_planner.py` | 1 story 1 active episode の管理 |
| `growth_engine.py` | キャラ成長候補の生成と commit |
| `quality_guard.py` | 出力品質の検査・retry・fallback |
| `prompt_builder.py` | LLM 向けプロンプト構築 |
| `interaction_patterns.py` | 面白い friction パターンの検出 |
| `relationship_modes.py` | 関係性の脚本的モード管理 |
| `emergent_canonizer.py` | 偶発的な良いズレの canon 化 |
| `dramatic_pressure.py` | pattern/mode/canon の上位解釈 |
| `story_intent.py` | story ごとの美学プロファイル |

## v2 upgrade で追加予定

- `chapter_manager.py` — Chapter System（ストーリー推進装置）
- `director_persona.py` — Director Persona（監修機能）

## 禁止事項

- LLM を直接呼ばない。必ず `LLMRouter` 経由
- DB を直接操作しない。必ず `DatabaseManager` 経由
- `print()` を使わない。`logging` を使う

## 関連 spec

- `docs/specs/simulation-engine.md`
- `docs/specs/runtime-architecture.md`

# db/ — データベースレイヤー

## モジュール境界

SQLite データベースの接続管理、スキーマ、マイグレーション、CRUD を担う。
エンジンや tools は `DatabaseManager` を唯一のエントリポイントとして使う。

## 構成

- `db_manager.py` — `DatabaseManager`（全 CRUD の窓口、migration 適用）
- `schema.sql` — 初期スキーマ定義
- `migrations/` — 連番 SQL（001〜021+、起動時に version 順で自動適用）

## 重要な前提

- 接続ごとに `PRAGMA journal_mode=WAL`, `PRAGMA foreign_keys=ON`, `PRAGMA busy_timeout=5000` を設定
- 全テーブルに `story_id` カラムあり（マルチストーリー対応）
- `aiosqlite` を使用。timeout 時は `sync_sqlite` backend にフォールバック
- 1 process あたり 1 connection を共有（別 thread / 別 process 共有前提ではない）

## migration 追加手順

1. `migrations/` に `{次の番号}_{説明}.sql` を作る
2. `DatabaseManager` の起動時に自動で適用される
3. 対応する CRUD メソッドを `db_manager.py` に追加する

## v2 upgrade で追加予定

- `migrations/022_chapters.sql` — Chapter System テーブル
- `migrations/023_director_persona.sql` — Director Persona テーブル

## 禁止事項

- `db_manager.py` を経由せず直接 SQL を実行しない
- 文字列結合で SQL を構築しない（パラメータバインディング必須）

## 関連 spec

- `docs/specs/data-model.md`

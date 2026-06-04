# PocketRole — AI エージェント向け完全ガイド

[日本語](AGENT_GUIDE.md) | [English summary](AGENT_GUIDE.en.md) | [简体中文摘要](AGENT_GUIDE.zh-CN.md)

更新日: 2026-06-04
対象バージョン: v0.1.0-beta.1

このドキュメントは、**AI エージェントが PocketRole を新規インストールから運用まで完全に自律で扱えるようにする**ことを目的とした完全ガイドです。人間ユーザーへの補助にも使えます。

> **Multilingual note**: This Japanese file is the canonical agent guide. The English and Simplified Chinese files are short summaries for orientation only.
>
> **多语言说明**：本日语文件是 AI Agent 操作指南的正本。英文版和简体中文版只是用于了解概要的摘要。
>
> **正本の優先順位**: コード > `docs/specs/` > このファイル。コードと食い違う場合はコードを優先してください。

---

## 目次

1. [PocketRole とは何か](#1-pocketrole-とは何か)
2. [システム全体像](#2-システム全体像)
3. [動作要件](#3-動作要件)
4. [インストール手順](#4-インストール手順)
5. [設定ファイル一覧](#5-設定ファイル一覧)
6. [ストーリー定義](#6-ストーリー定義)
7. [CLI コマンド一覧](#7-cli-コマンド一覧)
8. [Admin UI 操作ガイド](#8-admin-ui-操作ガイド)
9. [LLM プロバイダー設定](#9-llm-プロバイダー設定)
10. [公開 Web ビューワー（PHP）](#10-公開-web-ビューワーphp)
11. [BGM・ビジュアル設定](#11-bgmビジュアル設定)
12. [物語進行管理機能](#12-物語進行管理機能)
13. [テスト実行](#13-テスト実行)
14. [デプロイ（本番公開）](#14-デプロイ本番公開)
15. [トラブルシューティング](#15-トラブルシューティング)
16. [AI エージェントの安全原則](#16-ai-エージェントの安全原則)

---

## 1. PocketRole とは何か

PocketRole は、LLM を使ってキャラクターが自律的に会話・移動・感情変化し、物語を自動生成するローカルファーストの箱庭シミュレーションプラットフォームです。

### 主な価値

- **自作キャラ同士に自律的に物語を生成させられる**
- **生成された会話をリアルタイムで Web 公開できる**
- **クラウド LLM もローカル LLM（Ollama）も選べる**
- **Admin UI でブラウザからすべてを管理できる**

### 主なユースケース

- 小説・シナリオの下書き素材として
- キャラクター設定の動的な検証として
- 継続シミュレーション型の公開 Web コンテンツとして

---

## 2. システム全体像

```
[ユーザー / AI エージェント]
        │
        ├─ YAML 定義編集 ──→ [stories/<story_id>/*.yaml]
        └─ Admin UI ────────→ [admin/app.py] (aiohttp :8787)
                                      │
                              ┌───────┴────────┐
                              │                 │
                    [engine.main]        [stories/<story_id>/]
                    (asyncio)             (YAML + 画像 + BGM)
                              │
                    [SQLite DB] ←→ [WebPoster] ──→ [receiver.php]
                    pocketrole.db                        │
                                               [web/chatlog/*.dat]
                                                         │
                                               [PHP viewer/map_replay]
                                               (公開 Web ビューワー)
```

### 状態の境界（重要）

| レイヤー | 正本 | 役割 |
|---|---|---|
| 定義層 | `stories/*.yaml` | キャラ・場所・章・監督の定義 |
| ローカル runtime | `db/pocketrole.db` (SQLite) | 実行中の状態・ログ |
| 公開 Web 層 | `web/chatlog/*.dat` (JSONL) | public viewer が読むデータ |
| static assets | `web/assets/story_maps/` | 背景画像・BGM・マップ定義 |

**local DB にログがあっても `WebPoster` が `receiver.php` に送信するまで public viewer には出ません。**

---

## 3. 動作要件

| 項目 | 要件 |
|---|---|
| Python | 3.11 以上 |
| SQLite | 3（WAL モード） |
| PHP | 8.x（公開 Web viewer 用、省略可） |
| LLM | OpenAI API キー、または Ollama |
| OS | Linux / macOS（Windows は WSL2 推奨） |
| メモリ | 1GB 以上推奨（Ollama 使用時は追加） |

Python パッケージ（`pocketrole/requirements.txt`）:

```
aiohttp>=3.9.0
pyyaml>=6.0
python-dotenv>=1.0.0
aiosqlite==0.21.0
Pillow>=10.0.0
openai>=1.30.0
anthropic>=0.30.0
google-generativeai>=0.7.0
jsonschema>=4.20.0
feedparser>=6.0.11
pytest>=8.0.0
pytest-asyncio>=0.23.0
```

---

## 4. インストール手順

**すべてのコマンドは `pocketrole/` ディレクトリ内で実行します。**

### 4-1. 仮想環境作成

```bash
cd pocketrole
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 4-2. 設定ファイル作成

```bash
cp config.yaml.example config.yaml
cp config/llm_runtime.example.yaml config/llm_runtime.local.yaml
```

### 4-3. API キー設定（`.env`）

```bash
# .env を作成（引用符・行末コメント不要）
OPENAI_API_KEY=sk-your-openai-key
# 他プロバイダーを使う場合：
ANTHROPIC_API_KEY=sk-ant-your-key
GEMINI_API_KEY=your-gemini-key
DEEPSEEK_API_KEY=sk-deepseek-your-key
```

**注意: `.env` は絶対に Git にコミットしないこと。**

### 4-4. LLM runtime 設定

`config/llm_runtime.local.yaml` の `active_profile` を使いたいプロバイダーに変更します。

```yaml
active_profile: "openai_nano"   # OpenAI で始める場合
# active_profile: "ollama_local"  # Ollama の場合（example のデフォルト）
```

### 4-5. サンプルストーリーのインポート

```bash
.venv/bin/python -m tools.validate_story stories/ankoku_gakuen/
.venv/bin/python -m tools.import_story stories/ankoku_gakuen/ --db db/pocketrole.db
```

### 4-6. エンジン起動

```bash
.venv/bin/python -m engine.main --stories ankoku_gakuen
```

### 4-7. Admin UI 起動

```bash
POCKETROLE_ADMIN_USER="your-username" \
POCKETROLE_ADMIN_PASSWORD="strong-password-here" \
POCKETROLE_ADMIN_COOKIE_SECURE=0 \
.venv/bin/python -m admin.main
```

ブラウザで `http://127.0.0.1:8787/admin` を開きます。  
本番環境では `POCKETROLE_ADMIN_COOKIE_SECURE=1` にしてください。

### 4-8. 動作確認

```bash
# DB にログが入っているか確認
sqlite3 db/pocketrole.db \
  "select turn_number, speaker_name, substr(message,1,60) \
   from chat_logs where story_id='ankoku_gakuen' \
   order by id desc limit 10;"
```

---

## 5. 設定ファイル一覧

### ローカル専用ファイル（Git管理外・公開禁止）

| ファイル | 役割 |
|---|---|
| `pocketrole/.env` | API キー（絶対に公開しない） |
| `pocketrole/config/llm_runtime.local.yaml` | LLM プロバイダー・モデル設定 |
| `pocketrole/config/web_post_targets.local.yaml` | Web 投稿先 URL・トークン |
| `pocketrole/db/pocketrole.db` | SQLite DB（進行データ） |
| `pocketrole/db/news_articles.db` | 時事モード用記事 DB |

### 配布可能ファイル

| ファイル | 役割 |
|---|---|
| `pocketrole/config.yaml` | 全体設定（`config.yaml.example` からコピー） |
| `pocketrole/config/llm_runtime.example.yaml` | LLM 設定テンプレート |
| `pocketrole/stories/<story_id>/*.yaml` | ストーリー定義 |

### `config.yaml` 主要セクション

DB パスは `config.yaml` ではなく CLI の `--db` フラグ（または `POCKETROLE_ADMIN_DB` 環境変数）で渡します。デフォルトは `db/pocketrole.db` です。Admin の host/port も `config.yaml` ではなく環境変数（`POCKETROLE_ADMIN_HOST` / `POCKETROLE_ADMIN_PORT`）で指定します。

各 story-emergence 機能はトップレベルのセクションとして配置され、それぞれ `enabled:` を持ちます（`engine:` セクション配下ではありません）。

```yaml
llm:
  cloud_concurrency: 3          # クラウド API 同時実行数上限
  providers:
    openai:
      default_model: "gpt-5.4-nano"
      api_mode: "auto"          # auto / chat_completions / responses
      temperature: 0.8
    ollama:
      base_url: "http://localhost:11434"
      default_model: "ministral-3:14b"

engine:
  memory_limit: 20
  emotion_decay_rate: 0.05
  max_move_cost: 1

# ─── story-emergence 機能（各自 enabled を持つトップレベルセクション）───
story_director:
  enabled: true                 # 監督介入
character_evolution:
  enabled: true                 # キャラ進化
scene_management:
  enabled: false                # scene 管理
quality_guard:
  enabled: false                # 品質チェック
growth_engine:
  enabled: false                # 成長エンジン
participation_planner:
  enabled: false
story_hooks:
  enabled: false
relationship_dynamics:
  enabled: false
scene_script:
  enabled: false
```

> 時事モード（News Mode）は `config.yaml` ではなく Admin の `/admin/news-mode` と専用 DB（`db/news_articles.db`）で管理します。

---

## 6. ストーリー定義

ストーリーは `pocketrole/stories/<story_id>/` に配置します。

### ファイル構成

```
stories/ankoku_gakuen/
├── world_config.yaml    # 世界設定・場所・イベント・異変
├── characters.yaml      # キャラクター定義
├── chapters.yaml        # 章・Beat・Beat event 定義
└── director.yaml        # 監督ペルソナ定義
```

### `world_config.yaml` の主要フィールド

```yaml
story:
  id: "my_story"
  title: "ストーリータイトル"
  description: "概要"
  season_start: "2026-04-01"
  turn_minutes: 30              # 1ターン = 現実の何分
  turn_interval_sec: 45         # ターン間隔（秒）
  world_rules: |
    この世界のルール...

places:
  - id: "classroom"
    label: "教室"
    atmosphere: "雰囲気の説明"
    adjacent_places:
      corridor: 1               # 隣接場所 ID: 距離
```

### `characters.yaml` の主要フィールド

```yaml
characters:
  - id: "hoshikaze_runa"
    name: "星風ルナ"
    name_reading: "ほしかぜるな"
    story_id: "my_story"

    personality:                  # personality は文字列ではなくオブジェクト
      type: "天然属性の良心担当。唯一の音楽経験者でリーダー格に押し込まれている"
      first_person: "私"
      speech_style: "丁寧で柔らかい・天然的なズレたコメントが混じる"
      strengths:
        - "ピアノ演奏"
      weaknesses:
        - "リーダーとしての自覚がない"
      speech_examples:
        - "あの……それ、理科室の三脚だと思うんですけど……"
      never_say:
        - "任せといて、私が全部やります"

    goal: "サーカスの余興をやり遂げて学校を存続させる"
    worry: "リーダーの重責に本当に耐えられるのか不安"

    emotion_default:              # 各感情値は 0.0〜1.0
      stress: 0.52
      motivation: 0.65
      loneliness: 0.40
      excitement: 0.48

    favorite_places:
      - "music_room"

    expressions:                  # 用意する表情画像のキー
      - "neutral"
      - "happy"
      - "angry"
      - "sad"
      - "surprised"
      - "worried"
      - "content"
      - "lonely"
      - "tired"
      - "determined"

    behavior_notes:
      - "変な案が出るたびに、おずおずと正論を一言挟む"
```

> 完全なスキーマはサンプル `stories/ankoku_gakuen/characters.yaml` と `docs/specs/data-model.md` を正本として参照してください。上記は主要フィールドの抜粋です（`appearance`, `secret` などの任意フィールドもあります）。

### ストーリー ID・キャラ ID・場所 ID の命名規則

- すべて `snake_case`（小文字＋アンダースコア）
- 例: `ankoku_gakuen`, `luna`, `music_room`
- **ID 変更は高リスク**（DB・アセット・参照が壊れる可能性）

---

## 7. CLI コマンド一覧

**すべてのコマンドは `pocketrole/` ディレクトリ内で実行します。**

### ストーリー管理

```bash
# YAML バリデーション
.venv/bin/python -m tools.validate_story stories/<story_id>/

# 新規インポート（初回のみ）
.venv/bin/python -m tools.import_story stories/<story_id>/ --db db/pocketrole.db

# 進行中 story の YAML 変更を反映（ログを消さない）
.venv/bin/python -m tools.update_story stories/<story_id>/ --db db/pocketrole.db

# 章定義をインポート
.venv/bin/python -m tools.import_chapters --story <story_id> --db db/pocketrole.db

# 監督ペルソナをインポート
.venv/bin/python -m tools.import_director --story <story_id> --db db/pocketrole.db

# MAP 用 scaffold 生成（場所マップ JSON + 画像 placeholder）
.venv/bin/python -m tools.generate_story_map_scaffold stories/<story_id>/

# Web 投稿設定初期化
.venv/bin/python -m tools.init_web_post_targets --stories <story_id>
```

### エンジン操作

```bash
# エンジン起動（単一 story）
.venv/bin/python -m engine.main --stories <story_id>

# エンジン起動（複数 story）
.venv/bin/python -m engine.main --stories story1 story2

# smoke テスト（1ターンだけ生成して終了）
.venv/bin/python -m tools.smoke_engine_start --story <story_id> --db db/pocketrole.db --turns 1

# ログ確認
tail -f logs/engine.log
```

### Admin 操作

```bash
# Admin 起動
POCKETROLE_ADMIN_USER="username" \
POCKETROLE_ADMIN_PASSWORD="password" \
POCKETROLE_ADMIN_COOKIE_SECURE=0 \
.venv/bin/python -m admin.main

# API トークン発行（AI エージェント向け）
.venv/bin/python -m tools.admin_token create --db db/pocketrole.db --label "my-agent"

# API トークン一覧
.venv/bin/python -m tools.admin_token list --db db/pocketrole.db

# API トークン失効
.venv/bin/python -m tools.admin_token revoke --db db/pocketrole.db --token-id <token_id>
```

### 観測・レビュー

```bash
# runtime 状態モニタリング（Markdown に追記）
.venv/bin/python -m tools.monitor_engine_runtime --story <story_id>

# story レビュードラフト生成
.venv/bin/python -m tools.generate_story_review_draft --story <story_id>
```

---

## 8. Admin UI 操作ガイド

Admin は `http://127.0.0.1:8787/admin` で動作します。

### 認証

- ブラウザ: ユーザー名・パスワードでログイン（cookie 認証）
- AI エージェント: `Authorization: Bearer <token>` ヘッダー

### ページ一覧

| URL | 機能 |
|---|---|
| `/admin` | ダッシュボード（KPI・runtime カード・直近会話） |
| `/admin/viewer/<story_id>` | Local Viewer（iMessage 風チャット） |
| `/admin/map-replay/<story_id>` | MAP 再生モード（BGM・背景画像付き） |
| `/admin/stories/<story_id>` | ストーリー詳細・設定 |
| `/admin/characters/<story_id>` | キャラクター編集 |
| `/admin/places/<story_id>` | 場所編集・背景画像・BGM |
| `/admin/directors/<story_id>` | 監督ペルソナ管理 |
| `/admin/chapter-definitions/<story_id>` | 章・Beat 管理 |
| `/admin/onboarding/<story_id>` | ストーリー複製 Wizard |
| `/admin/settings` | Admin 設定・LLM runtime |
| `/admin/news-mode` | 時事モード設定 |

### Local Viewer の使い方

- iMessage 風チャット UI でキャラの会話を確認
- j/k キーで上下スクロール、r でリフレッシュ、f で最新へ
- 「追従モード」ON で自動スクロール
- MAP 再生タブで地図ビューに切り替え可

### MAP 再生モードの使い方

- キャラのアイコンが各場所を移動する様子をアニメーション再生
- 場所ごとの背景画像が表示される
- BGM が自動再生（場所移動時に場所固有 BGM に切り替わる）
- Admin の MAP 再生は polling を停止して確実に再生される

### ストーリー詳細ページ（`/admin/stories/<story_id>`）の主要カード

| カード | 機能 |
|---|---|
| Hero card | タイトル・ステータス・起動/停止 |
| 時事モード | News mode 設定・頻度 |
| 発話の長さ | 最大文字数設定 |
| ストーリー BGM | カスタム BGM アップロード・有効/無効 |
| Web 投稿先 | 公開 URL・トークン設定 |
| 公開管理 | アーカイブ・公開管理 |
| 危険ゾーン | story リセット |

### Onboarding Wizard でストーリーを複製する

1. `/admin/onboarding/ankoku_gakuen` を開く
2. 新しい story ID・タイトル・説明を入力
3. キャラ名・性格・目標・表情画像を設定
4. 複製実行 → `/admin/stories/<new_story_id>` へ自動遷移
5. 追加設定（場所・章・監督・BGM）を各編集ページで設定

---

## 9. LLM プロバイダー設定

### `config/llm_runtime.local.yaml` の構成

各 profile は `provider` と `model` だけを持ちます。temperature・max_tokens・base_url などの生成パラメータは profile ではなく `config.yaml` の `llm.providers.<provider>` 側で設定します。

```yaml
active_profile: "openai_nano"     # 使用する profile

profiles:
  ollama_local:
    provider: "ollama"
    model: "ministral-3:14b"

  openai_nano:
    provider: "openai"
    model: "gpt-5.4-nano"

  openai_mini:
    provider: "openai"
    model: "gpt-5-mini"

# story ごとに異なる profile を使う場合（story_id: profile 名 の平坦なマッピング）
story_overrides:
  my_story: "ollama_local"
```

利用可能な provider 名は `ollama` / `openai` / `anthropic` / `gemini` / `deepseek` です。anthropic / gemini / deepseek の profile も同じ形式で追加できます（例: `provider: "anthropic"`, `model: "claude-sonnet-4-20250514"`）。

### Ollama のセットアップ（ローカル LLM）

```bash
# Ollama インストール（Linux）
curl -fsSL https://ollama.ai/install.sh | sh

# モデルダウンロード
ollama pull ministral-3:14b

# Ollama 起動（バックグラウンド）
ollama serve &
```

`config/llm_runtime.local.yaml` の `active_profile` を `ollama_local` に変更してください。Ollama の `base_url` は `config.yaml` の `llm.providers.ollama.base_url`（デフォルト `http://localhost:11434`）で指定します。

### プロバイダー別 API キー（`.env`）

```
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
GEMINI_API_KEY=...
DEEPSEEK_API_KEY=sk-...
```

DeepSeek は OpenAI 互換ですが、PocketRole では独立した provider 名 `deepseek` として扱います（`provider: "deepseek"`）。base_url 等の接続設定は `config.yaml` の `llm.providers.deepseek` 側にあります。

---

## 10. 公開 Web ビューワー（PHP）

PHP 公開ビューワーは Web サーバー（Apache / nginx）に `pocketrole/web/` を配置して使います。

### Web サーバーへのファイル配置

```
/var/www/html/your-site/
└── web/         ← pocketrole/web/ の内容をコピー
    ├── viewer.php
    ├── map_replay.php
    ├── api.php
    ├── receiver.php
    ├── status.php
    ├── archive.php
    ├── config.php        ← 通常編集不要
    └── assets/
```

`web/chatlog/` と `web/config/` は PHP が書き込める権限にしてください。`web/config/` は同梱の `.htaccess` で直接アクセスを拒否します。

### 受信トークンの設定

`web/config/auth_token.txt.example` をコピーして、サーバー上で `web/config/auth_token.txt` を作成します。

```bash
cp web/config/auth_token.txt.example web/config/auth_token.txt
```

`auth_token.txt` の中身を十分に長いランダム文字列へ置き換えてください。この実 token ファイルは `.gitignore` 対象で、GitHub へ含めません。`web/config.php` は通常編集不要です。

### Web 投稿先の設定

`config/web_post_targets.local.yaml` を作成:

```yaml
ankoku_gakuen:
  enabled: true
  receiver_url: "https://your-site.com/receiver.php"
  auth_token: "your-secret-token"
```

または Admin UI の「Web 投稿先」カードから設定できます。

### 公開 URL

```
https://your-site.com/viewer.php?story_id=<story_id>
https://your-site.com/map_replay.php?story_id=<story_id>
https://your-site.com/api.php?story_id=<story_id>&action=latest
https://your-site.com/status.php?story_id=<story_id>
https://your-site.com/archive_index.php
```

---

## 11. BGM・ビジュアル設定

### ストーリー BGM

Admin の「ストーリー BGM」カード（`/admin/stories/<story_id>`）から設定します。

- MP3/M4A ファイルをアップロード
- 保存先: `web/assets/story_maps/<story_id>/story_bgm.mp3`
- 設定ファイル: `web/assets/story_maps/<story_id>/bgm_manifest.json`
- 未設定時は `web/assets/audio/pocketrole_bgm.mp3`（グローバルデフォルト）を使用

### 場所固有 BGM

Admin の場所編集ページ（`/admin/places/<story_id>`）の各場所カード下部から設定します。

- 場所に移動した際に自動的にその場所の BGM に切り替わる（MAP 再生中）
- 保存先: `web/assets/story_maps/<story_id>/place_bgm/<place_id>.mp3`
- 場所専用 BGM が未設定なら、ストーリー BGM にフォールバック

### `bgm_manifest.json` の形式

```json
{
  "story_id": "my_story",
  "enabled": true,
  "story_default": "story_bgm.mp3",
  "place_overrides": {
    "music_room": "place_bgm/music_room.mp3",
    "rooftop": "place_bgm/rooftop.mp3"
  },
  "mood_overrides": {}
}
```

### 場所背景画像

Admin の場所編集ページから PNG/JPG をアップロードします。

- 推奨サイズ: 1280×720px 以上（横長）
- 保存先: `web/assets/story_maps/<story_id>/images/<place_id>.jpg`
- MAP 再生中、その場所に移動すると背景が切り替わる

### キャラクター表情画像

Admin のキャラクター編集ページから、各キャラの表情画像を設定します。どの表情を使うかは `characters.yaml` の各キャラの `expressions:` リストで定義します。サンプル story の標準セットは次の 10 種です。

```
neutral.png       # 通常
happy.png         # 嬉しい
angry.png         # 怒り
sad.png           # 悲しい
surprised.png     # 驚き
worried.png       # 心配
content.png       # 満足
lonely.png        # 寂しい
tired.png         # 疲れ
determined.png    # 決意
```

保存先: `web/assets/character_images/<story_id>/<character_id>/<expression>.png`

---

## 12. 物語進行管理機能

### 監督ペルソナ（Director Persona）

Admin の `/admin/directors/<story_id>` で管理します。

- **監督**とは、物語全体の方向性・雰囲気・テーマを指揮するキャラクター的な役割
- 複数の監督ペルソナを定義し、状況に応じて切り替えられる
- 交代理由を記録することで、物語の転換点を管理できる

`director.yaml` の例（`personas` は id をキーにしたマップ。各 persona は美的傾向を表す `aesthetic` 数値群と `values` / `traits` を持ちます）:

```yaml
default_active: comedy_amplifier      # 起動時にアクティブにする persona id

personas:
  comedy_amplifier:                   # persona id をキーにする
    name: "コメディ増幅監督"
    aesthetic:                        # 各値 0.0〜1.0
      tension_preference: 0.45
      character_depth: 0.78
      action_preference: 0.60
      dialogue_wit: 0.95
      atmosphere_weight: 0.50
      curiosity: 0.65
    values:
      - "ボケとツッコミは最優先で発火させる"
    traits:
      - "ボケとツッコミのテンポ優先"
```

### 章・Beat システム（Chapter / Beat）

Admin の `/admin/chapter-definitions/<story_id>` で管理します。章は `theme` と `world_injection` を持ち、`beats` は `phase`（setup / complication など）ごとに `goal` と `events` を持ちます。

```yaml
chapters:
  - id: "ch1_invasion"
    title: "第一章「乗り込んできた経営陣」"
    theme: "対立と混乱 — 廃校通告から始まる悲喜劇"
    world_injection: >
      この章で世界に注入する状況説明...
    start_condition: "turn >= 0"
    beats:
      - phase: "setup"
        description: "廃校通告書が配布される"
        goal: "廃校という現実と生徒たちの混乱が伝わる"
        events:
          - type: "final_notice"
            flavor: "conflict"
            desc: "最終通告書が全生徒に届いた"
```

### 時事モード（News Mode）

Admin の `/admin/news-mode` で設定します。

- RSS フィードからリアルタイムニュースを取得
- キャラクターがニュースを話題に会話する
- 頻度設定: 20%（かなり高め）〜 50%（ほぼ毎回）

### イベント・異変（Events / Anomalies）

PocketRole の「出来事」と「異変」は、単一の `events:` / `anomalies:` リストではなく、複数の仕組みに分かれています。

- **場所ごとの出来事ヒント**: 各 place の `events_likely:` に、その場所で起きやすい出来事を文章で列挙します（プロンプトのヒントとして使われます）。
- **時間帯ごとの異変メモ**: 時間帯定義の `anomaly_note:` に、その時間に特定の場所にいると「異常」とみなす状況を書きます。
- **異変ルール（anomaly_rules）**: `label` と `condition_json`（条件）の組で、特定条件が満たされたときに発火する異変を定義します。条件は Admin の **condition builder**（`/admin/stories/<story_id>` のイベント/異変編集）で組むのが推奨です。

```yaml
# world_config.yaml の place 内（出来事ヒント）
places:
  - id: "classroom"
    events_likely:
      - "授業中の居眠りとサーカス対策メモ"
      - "焼坂組長の新しい封書が回ってくる"
    # 時間帯定義側に anomaly_note を持たせる場所もある
```

> 異変ルールの正確なスキーマ（`condition_json` の構造）はサンプル story と `docs/specs/data-model.md` を参照してください。手書きより Admin の condition builder の使用を推奨します。

### Scene Management・Growth Engine

`config.yaml` のトップレベルセクション（各 `enabled:` を持つ）で opt-in で有効化します。`engine:` セクション配下ではない点に注意してください。

```yaml
scene_management:
  enabled: true             # 場所ごとの集まり管理
quality_guard:
  enabled: true             # 長文・反復語・装飾記号チェック
growth_engine:
  enabled: true             # キャラの人格成長
story_director:
  enabled: true             # 監督介入
```

時事モード（News Mode）は `config.yaml` ではなく Admin の `/admin/news-mode` で設定します。

---

## 13. テスト実行

```bash
cd pocketrole

# 全テスト（integration テストは除外）
.venv/bin/pytest tests/ -v --ignore=tests/test_integration.py

# 特定ファイルのみ
.venv/bin/pytest tests/test_admin_ui_assets.py -v

# 簡易実行
.venv/bin/pytest tests/ -q --ignore=tests/test_integration.py
```

現在のテスト数: **1573 件**（すべて通過）

---

## 14. デプロイ（本番公開）

### systemd + nginx 構成（推奨）

`deploy/` ディレクトリにテンプレートがあります。

```bash
# PocketRole engine の systemd サービス登録例
sudo cp deploy/systemd/pocketrole-engine.service /etc/systemd/system/
sudo systemctl enable pocketrole-engine
sudo systemctl start pocketrole-engine

# Admin の systemd サービス登録例
sudo cp deploy/systemd/pocketrole-admin.service /etc/systemd/system/
sudo systemctl enable pocketrole-admin
sudo systemctl start pocketrole-admin
```

### nginx の設定例

```nginx
# Admin（reverse proxy）
server {
    listen 443 ssl;
    server_name admin.example.com;

    location /admin {
        proxy_pass http://127.0.0.1:8787;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
    }
}

# Public viewer（PHP）
server {
    listen 443 ssl;
    server_name example.com;
    root /var/www/html/your-site;
    index viewer.php;

    location ~ \.php$ {
        fastcgi_pass unix:/run/php/php8.2-fpm.sock;
        include fastcgi_params;
        fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;
    }
}
```

### 本番環境の Admin 起動

```bash
POCKETROLE_ADMIN_USER="secure-username" \
POCKETROLE_ADMIN_PASSWORD="very-strong-password" \
POCKETROLE_ADMIN_COOKIE_SECURE=1 \
POCKETROLE_ADMIN_HOST="127.0.0.1" \
.venv/bin/python -m admin.main
```

### セキュリティチェックリスト（公開前）

- [ ] `.env` が `.gitignore` に含まれている
- [ ] `db/pocketrole.db` が `.gitignore` に含まれている
- [ ] `config/*.local.yaml` が `.gitignore` に含まれている
- [ ] `POCKETROLE_ADMIN_PASSWORD` が十分に強い
- [ ] `web/config/auth_token.txt` と `web_post_targets.local.yaml` の `auth_token` が十分に強い
- [ ] nginx で HTTPS が設定されている
- [ ] Admin が直接 Internet に露出していない（reverse proxy 越し）
- [ ] `web/chatlog/` が直接リストされない（nginx で制限）

---

## 15. トラブルシューティング

### エンジンが起動直後に終了する

1. `.env` に API キーがあるか確認
2. `config/llm_runtime.local.yaml` の `active_profile` が正しいか確認
3. `logs/engine.log` のエラー行を確認
4. smoke テストで 1 ターン生成できるか確認:
   ```bash
   .venv/bin/python -m tools.smoke_engine_start --story ankoku_gakuen --db db/pocketrole.db --turns 1
   ```

### Public viewer に何も出ない

- `WebPoster` が `receiver.php` に送信しているか確認
- `config/web_post_targets.local.yaml` の URL・token が正しいか確認
- `db/pocketrole.db` の `chat_logs` で `posted_to_web=1` の行があるか確認:
  ```bash
  sqlite3 db/pocketrole.db "select count(*) from chat_logs where posted_to_web=1"
  ```

### Admin が `401 Unauthorized` になる

- 環境変数 `POCKETROLE_ADMIN_USER` と `POCKETROLE_ADMIN_PASSWORD` が設定されているか確認
- `admin/admin` のデフォルト認証情報は意図的に無効化されています

### MAP 再生で画像・BGM が出ない

1. `web/assets/story_maps/<story_id>/` に画像と `bgm_manifest.json` があるか確認
2. ブラウザのキャッシュをクリア（Hard Reload またはプライベートウィンドウ）
3. Admin MAP 再生タブを使用中に parent viewer の polling が止まっているか確認

### Ollama 接続エラー

```bash
# Ollama が起動しているか確認
curl http://localhost:11434/api/tags

# 起動していない場合
ollama serve &
```

### `import_story --force-replace` の誤使用

**`--force-replace` は進行中の story には使わないでください。**  
進行ログ・状態・session・chapter/director runtime state が削除されます。  
進行中の YAML 変更は `update_story` を使います。

---

## 16. AI エージェントの安全原則

AI エージェントが PocketRole を操作する際の必須ルールです。

### 絶対禁止

1. **`.env` を Git にコミットしない**
2. **進行中 story に `import_story --force-replace` を使わない**（ログが消える）
3. **runtime が動いている story を直接編集しない**（停止してから行う）
4. **`db/pocketrole.db` を直接削除しない**（admin の story reset を使う）
5. **`POCKETROLE_ADMIN_PASSWORD` をコードにハードコードしない**

### 推奨する作業順序

```
1. git status --short で既存差分を確認
2. 変更対象を特定し、影響範囲を見積もる
3. admin UI で対応可能なら admin を優先
4. YAML 編集後は必ず validate_story を実行
5. 変更後は pytest でテストを確認
6. 破壊的操作は必ずユーザーに確認を取る
```

### ID 変更時の確認事項

| 変更対象 | 影響範囲 |
|---|---|
| story_id | DB 全テーブル・アセットパス・公開 URL |
| character ID | DB・画像ディレクトリ・YAML 参照 |
| place ID | DB・画像・BGM・MAP JSON |
| chapter ID | DB・director.yaml 参照 |

ID 変更は Admin の rename 導線がある場合はそれを優先してください。

### API エンドポイント（AI エージェント向け）

Admin API は `Authorization: Bearer <token>` で認証します。

```
GET  /admin/healthz                                       # ヘルスチェック（認証不要）
GET  /admin/api/v1/stories                                # ストーリー一覧
GET  /admin/api/v1/stories/<story_id>                     # ストーリー詳細
GET  /admin/api/v1/stories/<story_id>/live                # runtime ライブ状態
PUT  /admin/api/v1/stories/<story_id>/runtime             # 起動/停止（body: {"desired_state":"running"|"stopped"}）
POST /admin/api/v1/stories/<story_id>/runtime/restart     # 再起動
POST /admin/api/v1/stories/<story_id>/runtime/reset       # 進行リセット（confirm 必須）
GET  /admin/api/v1/stories/<story_id>/bgm-config          # BGM 設定取得
PUT  /admin/api/v1/stories/<story_id>/bgm-config          # BGM 設定更新
```

---

## 付録: ディレクトリ構造（完全版）

```
pocketrole/
├── engine/                      # シミュレーションエンジン
│   ├── main.py                  # 起動入口
│   ├── story_engine.py          # メイン生成ロジック
│   ├── story_director.py        # 監督介入
│   ├── growth_engine.py         # キャラ成長
│   ├── quality_guard.py         # 品質チェック
│   ├── episode_planner.py       # エピソード計画
│   └── llm/                     # LLM プロバイダー群
│       ├── base.py              # 抽象基底クラス
│       ├── router.py            # LLMRouter（振り分け）
│       ├── openai_client.py
│       ├── anthropic_client.py
│       ├── gemini_client.py
│       ├── ollama_client.py
│       └── deepseek_client.py
├── admin/                       # 管理 UI・管理 API
│   ├── main.py                  # Admin 起動
│   ├── app.py                   # aiohttp アプリ・全 API エンドポイント
│   └── assets/
│       ├── admin_app.js         # Admin SPA 本体（vanilla JS）
│       ├── admin_style.css      # Admin 固有スタイル
│       ├── tokens.css           # デザイントークン
│       ├── components.css       # 共通コンポーネント
│       ├── layout.css           # レイアウト
│       └── ui/                  # theme.js, toast.js, icons.js, skeleton.js
├── web/                         # 公開 PHP ビューワー
│   ├── viewer.php               # タイムライン viewer
│   ├── map_replay.php           # MAP 再生 viewer
│   ├── api.php                  # 公開 API
│   ├── receiver.php             # WebPoster 受信
│   ├── status.php               # 公開 status
│   ├── archive.php              # アーカイブ viewer
│   ├── config.php               # PHP 設定
│   └── assets/
│       ├── map_replay.js        # MAP 再生 SPA（Phaser 3 + vanilla）
│       ├── audio/
│       │   └── pocketrole_bgm.mp3  # グローバルデフォルト BGM
│       ├── character_images/    # キャラ表情画像
│       └── story_maps/          # 場所背景・BGM・MAP 定義
│           └── <story_id>/
│               ├── map.json         # MAP レイアウト
│               ├── place_manifest.json
│               ├── bgm_manifest.json
│               ├── story_bgm.mp3    # ストーリー BGM
│               ├── images/          # 場所背景画像
│               └── place_bgm/       # 場所固有 BGM
├── stories/                     # ストーリー定義
│   └── ankoku_gakuen/           # サンプルストーリー
│       ├── world_config.yaml
│       ├── characters.yaml
│       ├── chapters.yaml
│       └── director.yaml
├── db/                          # DB 管理
│   ├── db_manager.py
│   ├── schema.sql
│   └── migrations/              # SQL マイグレーション
├── tools/                       # CLI ツール群
├── tests/                       # pytest テスト
├── config.yaml                  # 全体設定
├── config.yaml.example          # 設定テンプレート
└── requirements.txt             # Python 依存
```

---

*このドキュメントは PocketRole v0.1.0-beta.1 時点の内容です。最新情報は `docs/specs/` を参照してください。*

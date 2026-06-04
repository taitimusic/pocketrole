# PocketRole

[日本語](README.md) | [English](README.en.md) | [简体中文](README.zh-CN.md)

**キャラ駆動ストーリー箱庭プラットフォーム**  
A character-driven sandbox story simulation platform powered by LLMs.

> ベータ版 v0.1.0-beta.1 — 活発に開発中です。破壊的変更が入ることがあります。
>
> PocketRole は日本語ファーストで開発されています。英語・簡体字中国語の UI / ドキュメント対応は v0.1.0-beta.1 時点では部分対応です。

---

## PocketRole とは

PocketRole は、**LLM（大規模言語モデル）を使ってキャラクターが自律的に会話・移動・感情変化し、物語を自動生成する箱庭シミュレーション**です。

キャラクターの定義（性格・口調・目標・悩み）と世界設定（場所・ルール）を YAML で書くだけで、後は LLM が自律的に物語を紡ぎます。生成されたストーリーはリアルタイムで Web 公開できます。

```
あなたの世界設定 ──→ PocketRole ──→ 自律的なキャラの会話・行動
                                   ──→ Web 公開タイムライン
                                   ──→ MAP 再生ビューワー（BGM付き）
```

---

## 特徴

### キャラ自律生成
- キャラクターが場所を移動しながら自律的に会話・感情変化
- 10 段階の表情画像に連動した感情表現
- キャラ間の関係性が変化し成長する（Growth Engine）

### 複数 LLM プロバイダー対応
| プロバイダー | 接続方式 |
|---|---|
| OpenAI（GPT-4o, GPT-4.1, GPT-5系） | API |
| Anthropic（Claude） | API |
| Google Gemini | API |
| DeepSeek | API（OpenAI互換） |
| Ollama | ローカル（無料・無制限） |

同一 story 内で複数プロバイダーを使い分けることも可能です。

### 管理 UI（Admin Dashboard）
- **ストーリー・キャラ・場所をブラウザから GUI 編集**
- **Local Viewer**：iMessage 風チャット UI で会話を確認
- **MAP 再生モード**：キャラの移動を地図上でビジュアル再生（BGM・背景画像付き）
- **Onboarding Wizard**：サンプルをブラウザから数分で複製

### 物語進行管理
- **監督ペルソナ（Director Persona）**：監督キャラが物語の方向を指揮
- **章・Beat システム**：章ごとにストーリーの目標・緊張度を設定
- **時事モード（News Mode）**：RSS ニュースを取得してキャラが反応
- **イベント・異変（Events / Anomalies）**：条件付きで物語に介入

### BGM・ビジュアル
- ストーリーごとのカスタム BGM アップロード
- 場所ごとの固有 BGM（MAP 再生中に場所移動で自動切り替え）
- 場所ごとの背景画像（PNG/JPG）

### 公開 Web ビューワー（PHP）
- タイムライン表示（公開 URL を共有するだけで誰でも閲覧可能）
- MAP 再生ビューワー（公開 URL）
- アーカイブ（過去の会話ログを静的 JSON で公開）

---

## 動作要件

| 項目 | 要件 |
|---|---|
| Python | 3.11 以上 |
| データベース | SQLite 3 |
| Web サーバー（公開用） | PHP 8.x（省略可） |
| LLM | OpenAI API キー、または Ollama（ローカル） |
| OS | Linux / macOS（Windows は WSL 推奨） |

---

## クイックスタート

### 1. リポジトリをクローン

```bash
git clone https://github.com/YOUR_USERNAME/pocketrole.git
cd pocketrole
```

### 2. Python 環境のセットアップ

```bash
cd pocketrole
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 3. 設定ファイルを作成

```bash
cp config.yaml.example config.yaml
cp config/llm_runtime.example.yaml config/llm_runtime.local.yaml
```

### 4. OpenAI API キーを設定

```bash
# .env ファイルを作成
echo "OPENAI_API_KEY=sk-your-key-here" > .env
```

`config/llm_runtime.local.yaml` の `active_profile` を `openai_nano` に変更します。

### 5. サンプル story を起動

```bash
.venv/bin/python -m tools.validate_story stories/ankoku_gakuen/
.venv/bin/python -m tools.import_story stories/ankoku_gakuen/ --db db/pocketrole.db
.venv/bin/python -m engine.main --stories ankoku_gakuen
```

### 6. Admin で確認

```bash
POCKETROLE_ADMIN_USER="admin" \
POCKETROLE_ADMIN_PASSWORD="your-password" \
POCKETROLE_ADMIN_COOKIE_SECURE=0 \
.venv/bin/python -m admin.main
```

ブラウザで `http://127.0.0.1:8787/admin` を開き、`ankoku_gakuen` の viewer でキャラの会話が流れていれば成功です。

詳細な手順は [START_HERE.md](START_HERE.md) を参照してください。

---

## 自作ストーリーを作る

Admin の Onboarding Wizard でサンプルを複製するのが最も簡単です。

```
http://127.0.0.1:8787/admin/onboarding/ankoku_gakuen
```

ブラウザから以下を設定できます：

- ストーリータイトル・説明
- キャラクター（名前・性格・口調・表情画像）
- 場所（名前・雰囲気・隣接関係・背景画像）
- 章・Beat・監督ペルソナ
- BGM（ストーリー全体・場所ごと）
- 公開設定（Web 投稿先 URL・トークン）

---

## ドキュメント

| ファイル | 内容 |
|---|---|
| [START_HERE.md](START_HERE.md) | 初回起動の最短ルート |
| [AGENT_GUIDE.md](AGENT_GUIDE.md) | AI エージェント向け完全ガイド |
| [AGENT_GUIDE.en.md](AGENT_GUIDE.en.md) | AI agent guide summary |
| [AGENT_GUIDE.zh-CN.md](AGENT_GUIDE.zh-CN.md) | AI Agent 操作指南摘要 |
| [README.en.md](README.en.md) | English overview |
| [README.zh-CN.md](README.zh-CN.md) | 简体中文概要 |
| [docs/specs/](docs/specs/) | 実装仕様書群 |
| [docs/runbooks/](docs/runbooks/) | 運用手順書 |
| [docs/users_manual/USER_MANUAL.md](docs/users_manual/USER_MANUAL.md) | ユーザーマニュアル |

---

## プロジェクト構成

```
pocketrole_project/
├── pocketrole/               # アプリ本体
│   ├── engine/               # シミュレーションエンジン（Python asyncio）
│   ├── admin/                # 管理 UI・管理 API（aiohttp）
│   ├── web/                  # 公開 Web ビューワー（PHP）
│   ├── stories/              # ストーリー定義 YAML + 画像
│   ├── db/                   # SQLite スキーマ・マイグレーション
│   ├── tools/                # CLI ユーティリティ
│   └── tests/                # pytest テスト
├── deploy/                   # デプロイ設定（nginx, systemd）
├── docs/                     # 仕様書・手順書・設計ノート
├── START_HERE.md             # 初回導入ガイド
├── AGENT_GUIDE.md            # AI エージェント向け完全ガイド
└── README.md                 # このファイル
```

---

## ライセンス

MIT License — 詳細は [LICENSE](LICENSE) を参照してください。

同梱しているサードパーティコンポーネントの通知は [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) を参照してください。

---

## 開発状況

- **バージョン**: v0.1.0-beta.1
- **ブランチ**: `upgrade-v2`
- **テスト**: pytest 1500+ 件の自動テストで主要機能を検証
- **状態**: ベータ — 機能は動作しますが、API・設定形式が変わる可能性があります

---

## フィードバック・貢献

Issue や Pull Request を歓迎します。  
使い方の質問・不具合報告は GitHub Issues へどうぞ。

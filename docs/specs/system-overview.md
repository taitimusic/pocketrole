# PocketRole システム全体像

更新日: 2026-03-24

## 目的

PocketRole の現在構成を 1 枚で把握できるようにし、次の実装作業で「どこが source of truth か」を迷わないようにする。

## この文書の正本範囲

- `engine/*.py`
- `db/schema.sql`, `db/migrations/*.sql`
- `web/*.php`
- `stories/*/*.yaml`

## 更新基準

コンポーネント分割や data flow が変わったときに更新する。

## 関連資料

- [runtime-architecture.md](./runtime-architecture.md)
- [data-model.md](./data-model.md)
- [web-surface.md](./web-surface.md)

## アプリの目的

PocketRole は、story 定義を元に複数キャラクターの会話・移動・感情変化を継続シミュレーションし、その結果を local DB に保存しつつ、公開 viewer 向けに Web 配信するローカル first の物語生成アプリである。

現在の主な使い方は次の 3 つに分かれる。

- local engine を回して `chat_logs` と `character_states` を蓄積する
- hosted PHP surface で公開タイムライン / map replay として閲覧する
- hosted admin service で runtime と publication を管理する

## 主要コンポーネント

### Story definition

`stories/<story_id>/world_config.yaml` と `characters.yaml` が定義入力である。  
ここには story 設定、場所、時間帯、異常ルール、キャラクター基本設定が入る。  
`characters.yaml` の `personality` では `speech_style` だけでなく、`speech_examples` と `never_say` も持てる。

### Local engine

Python 側の core は次のコンポーネントで構成される。

- `engine.main`
  - 起動入口。config 読み込み、DB 初期化、LLMRouter 起動、WebPoster 起動、ProcessManager 起動を行う。
  - runtime LLM の日常切替は `config/llm_runtime.local.yaml` を正本にする。
- `ProcessManager`
  - story ごとに `StoryEngine` task を立てる。
- `StoryEngine`
  - 1 キャラ 1 ターン生成を基本単位としつつ、feature flag 有効時は scene planning, hook dynamics, growth, director, scene-close prose を束ねる。
- `LLMRouter`
  - provider ごとの client と concurrency 制御をまとめる。
- `WebPoster`
  - local `chat_logs` の未送信分を hosted `receiver.php` へ送る。
  - 投稿先は story YAML ではなく `config/web_post_targets.local.yaml` の story 単位設定から解決する。
  - hosted admin runtime でも `engine.main` と同じ story 別設定解決を使う。
- `DatabaseManager`
  - SQLite connection と migration、CRUD の窓口である。

### Story-emergence runtime layer

current code には、`StoryEngine` の上に次の story-emergence 層がある。

- `scene_management`
  - place ごとの `story_scenes` / `scene_participants` を維持する
- `participation_planner`
  - 毎ターン全員発話ではなく、scene ごとに focus / support / observer を選ぶ
- `story_hooks` / `relationship_dynamics`
  - 発話後に未解決課題と関係変化を durable に残す
- `quality_guard`
  - 過長、抽象語反復、装飾記号などを検査して retry / shorten / drop を判断する
- `growth_engine`
  - `story_memory`, `story_hooks`, `relationship_events` などから人格 overlay を更新する
- `story_director` / `novel_generator`
  - scene close と tension evidence から介入と prose を生成する

ただしこれらは current 運用では **opt-in runtime** であり、`config.yaml` に対応セクションを明示しないと旧来挙動のまま動きうる。

### Local persistence

現在の local 正本は SQLite DB である。  
story 定義は import 後に DB へ展開され、実行時の状態は `stories`, `character_states`, `chat_logs`, `conversation_sessions` などに保存される。  
キャラの話し方制約は `characters.speech` JSON に保存され、`first_person`, `speech_style`, `examples`, `never_say` を持ちうる。  
story-emergence 系では `story_scenes`, `story_hooks`, `relationship_events`, `generation_quality_issues`, `character_profile_overlays`, `character_growth_candidates` なども local runtime state として保持される。

### Hosted public surface

public viewer の正本は local DB ではなく、hosted 側の `web/chatlog/<story_id>/*.dat` JSONL である。  
この `.dat` は `receiver.php` が受け取り、`viewer.php`, `map_replay.php`, `api.php`, `status.php` はこの JSONL を読む。
admin の story reset は、story の Web 投稿先が設定されている場合に限り、local DB reset の前に `receiver.php` へ authenticated reset request を送り、hosted 側 `*.dat` を story 単位で削除する。

`map_replay.php` の補助 asset は `web/assets/story_maps/{story_id}/` 配下にあり、`place_manifest.json` と `images/` scaffold は story lifecycle 側で生成される。

### Hosted admin surface

hosted admin は `aiohttp` の `/admin` 面として別 process で動く。
runtime control, archive build, publication metadata は local SQLite を正本とし、public PHP surface とは trust boundary を分ける。

### External observers

runtime 外部からの観測と review 用に、current code には次の observer CLI がある。

- `tools.monitor_engine_runtime`
  - 実 DB / engine process / Ollama / GPU / story-quality 指標を Markdown へ追記する
- `tools.generate_story_review_draft`
  - 実 DB と monitor Markdown から `docs/system_review/generated/` 向け review draft を生成する

## 現在の state 境界

### Local 側

- 正本: SQLite DB
- 用途: シミュレーション進行、再開、内部状態、LLM メタデータ

### Hosted 側

- 正本: story ごとの JSONL `.dat`
- 用途: 公開タイムライン、`status.php`, `api.php`, `viewer.php`

この分離のため、local DB にログがあっても `posted_to_web=1` になる前は hosted viewer には出ない。

## ストーリー単位の扱い

PocketRole の主要な execution unit は story である。  
1 story ごとに次のものを持つ。

- 1 つの `stories` 行
- 複数の `places`, `characters`, `relationships`
- 1 つの `StoryEngine` 実行 task
- 0 または 1 つの `WebPoster`
- 1 つの hosted chatlog directory
- 必要に応じて 1 つの hosted story map asset directory

ただし `DatabaseManager` と `LLMRouter` は複数 story で共有される。

## LLM provider の位置づけ

現実装で provider 名として扱うのは次の 5 つである。

- `ollama`
- `openai`
- `anthropic`
- `gemini`
- `deepseek`

日常運用の切替点は `config/llm_runtime.local.yaml` で、`active_profile` と `story_overrides` により same process 内の story ごとの runtime provider/model を決める。
story YAML や `stories.llm_provider` / `llm_model` は current runtime では切替元として使わない。

OpenAI provider の current behavior は次のとおり。

- API key は `.env` / process env から Config へ取り込み、`LLMRouter` は `os.environ` ではなく Config 内の `api_key` を使って client を生成する
- `providers.openai.api_mode` は `auto | chat_completions | responses`
- `api_mode=auto` では `gpt-5*` 系を `Responses API`、それ以外を `Chat Completions` へ自動で振り分ける
- current code では `gpt-5-mini`, `gpt-5.4-mini`, `gpt-5.4-nano` の smoke 成功を確認済みである
- `/v1/models` の可視性だけでは生成 endpoint 互換を保証しないため、model 切替時は `smoke_openai_models` 等で 1 ターン以上の生成 smoke を取る
- runtime file で未使用 provider を外していれば、OpenAI run で Ollama の起動状態を気にせず、Ollama run で OpenAI key を気にせず切り替えられる

## 現在の利用前提

- 開発と本番運用は local first で、公開 viewer / replay は PHP 側の軽量 surface として分離されている
- local engine は Python 非同期 task 群で回る
- story-emergence runtime は code 上は実装済みだが、`scene_management`, `participation_planner`, `story_hooks`, `relationship_dynamics`, `quality_guard`, `growth_engine` などは opt-in feature flag である
- hosted viewer は DB には直接触れず、JSONL だけを読む
- `map_replay.php` は `api.php?action=replay` と `web/assets/story_maps/{story_id}/` 資産を使う別 viewer である
- story ごとの Web 投稿先 URL / token は `config/web_post_targets.local.yaml` の environment-local 設定であり、story 定義 YAML には含めない
- story ごとの runtime LLM 切替も `config/llm_runtime.local.yaml` の environment-local 設定であり、story 定義 YAML には通常の運用変更を持ち込まない
- hosted admin は `/admin` にあり、publication metadata や archive build を扱う

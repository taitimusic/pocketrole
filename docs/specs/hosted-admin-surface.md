# PocketRole Hosted Admin Surface

更新日: 2026-05-06

## UI 刷新 (upgrade-v2 branch, 2026-05 完了)

admin UI は Phase 0〜10 の UI/UX 刷新を完了しました。主な変更:

- **デザインシステム**: `tokens.css` / `components.css` / `layout.css` によるトークン統一。ライト/ダーク両対応。
- **アプリシェル**: sticky topbar (ロゴ・テーマトグル・ハンバーガー) + sticky sidebar (active 状態ハイライト) + breadcrumb。`≤1024px` でサイドバーが drawer になる。
- **ダッシュボード**: KPI 行 (sparkline 付き) + runtime カード + 直近会話プレビュー + 監査ログストリーム。5 秒自動更新。
- **全エディタ**: skeleton loading → sticky save bar → `setSavingButton` / `showToast` フィードバック → `beforeunload` dirty guard。
- **Viewer**: iMessage 風チャットバブル (キャラ別 6 色) + 追従バナー + j/k/r/f キーボードショートカット。
- **設定画面**: Archive / LLM Runtime セクション分割。LLM プロファイルをラジオカードで選択。
- **監査ログ**: story_id / action フィルタ + 相対時刻 tooltip + ページネーション。
- **a11y**: `:focus-visible` ユニバーサルリング、`aria-expanded` / `aria-current` / `aria-label` を主要インタラクティブ要素に付与。375 / 480 / 768 / 1024 px ブレークポイント対応。
- **UI locale**: 管理 UI の固定ラベルは `ja`, `en`, `zh-TW` の翻訳カタログを通す。API value / enum value / story content は翻訳せず、表示ラベルだけ locale で切り替える。

ファイル構成:
```
admin/assets/
  tokens.css, components.css, layout.css   -- デザインシステム
  admin_style.css                          -- ページ固有スタイル
  admin_app.js                             -- SPA 本体 (vanilla JS)
  ui/theme.js, toast.js, icons.js, skeleton.js
```

## 目的

hosted 配備時の管理 UI / 管理 API / archive publish loop の current behavior を整理する。

## この文書の正本範囲

- `admin/app.py`
- `admin/main.py`
- `admin/runtime_controller.py`
- `tools/admin_token.py`

## 管理面の URL 境界

- 管理 UI と管理 API は `/admin` 配下で動く
- 正本の公開形は同一ドメイン配下の `/admin`
- 本番では reverse proxy 越しに公開し、`aiohttp` 自体は loopback bind を前提とする

## 認証

### ブラウザ

- `POST /admin/api/v1/session/login`
- `admin_session` cookie を発行する
- hosted 正本では `HttpOnly`, `Secure`, `SameSite=Lax`

### AI エージェント

- `Authorization: Bearer <token>`
- token は `tools/admin_token.py` で発行・列挙・失効する
- DB には hash だけ保存し、平文 token は発行時に一度だけ表示する

## `GET /admin/healthz`

- 認証不要
- 秘密情報は返さない
- 最低限、`service`, `db.status`, `runtime.states`, `publisher.status` を返す

## runtime control

- story 単位で `running / stopped / restart` を操作する
- hosted control plane は `HostedRuntimeController` が担当する
- `LLMRouter` は管理サービス process 内で共有する

## settings

- `/admin/settings`
  - archive publish interval を編集する
  - `config/llm_runtime.local.yaml` の active_profile / profiles / story_overrides を編集する
  - LLM runtime profile 更新は stopped story の次回 start から反映される
  - running story は restart 後に新しい profile を使う

## local viewer / onboarding / character editor

admin UI は `/admin` 配下で次の local-first 導線を持つ。

- `/admin/viewer/{story_id}`
  - local DB の recent chat logs を表情画像つきで表示する
  - public `viewer.php` とは別で、Web 投稿が未設定でも local DB にログがあれば確認できる
- `/admin/onboarding/{story_id}`
  - sample story を複製して custom story を作る
  - `story_id`, title, description, キャラ基本項目, `neutral.png` を差し替える
- `/admin/characters/{story_id}`
  - clone 後の custom story 向けの最小キャラ編集画面
  - 編集対象は character_id、表示名、短い説明、goal、worry、10表情PNG
  - 新規追加: `char_id` を空、`new_char_id` を指定して送ると最小有効な character entry を追加する
  - 削除: payload から既存 `char_id` を省略すると削除扱いにし、`update_story` により DB 上は inactive になる
  - character_id 変更(rename): 既存 `char_id` と `new_char_id` を送ると、`characters.yaml` の `characters[].id` と character image asset directory を更新する
  - `ankoku_gakuen` などの template story は read-only
  - runtime が `stopped` の story だけ保存できる
  - 保存時に `characters.yaml` を更新し、`validate_story`, `update_story`, story metadata export を実行する
- `/admin/places/{story_id}`
  - clone 後の custom story 向けの場所編集画面
  - 編集対象: 既存 place の label, zone, atmosphere, who_gathers, events_likely, access_note, adjacent_places
  - 新規追加: place_id(snake_case), label, zone が必須。adjacent_places は任意
  - 削除: payload から key を省略すると削除。favorite_places / time_schedules / force_place / condition_json.place / 他 place の adjacent_places から参照されている場合は 400 で reject し参照箇所一覧を返す
  - place_id 変更(rename): 既存 `place_id` と `new_place_id` を送ると、world_config.yaml / characters.yaml 内の favorite_places, time_schedules.expected_places, event_calendar.force_place, anomaly_rules.condition_json.place / expected_place, adjacent_places を更新する
  - `ankoku_gakuen` などの template story は read-only
  - runtime が `stopped` の story だけ保存できる
  - 保存時に `world_config.yaml` を更新し、`validate_story`, `update_story`, story metadata export を実行する
  - PUT の戻り値に `places_added`, `places_removed`, `places_renamed` を含む
- `/admin/story-settings/{story_id}`
  - clone 後の custom story 向けの最小 story 基本設定画面
  - 編集対象は title, description, season_start, turn_minutes, turn_interval_sec, world_rules
  - story_id と LLM 設定は扱わない
  - `ankoku_gakuen` などの template story は read-only
  - runtime が `stopped` の story だけ保存できる
  - 保存時に `world_config.yaml` を更新し、`validate_story`, `update_story`, story metadata export を実行する
- `/admin/event-anomalies/{story_id}`
  - clone 後の custom story 向けのイベント/異変編集画面
  - 既存エントリの基本項目編集: event_date, name, duration_days, atmosphere, emotion_impact, force_place / label, condition_json, drama_potential, suggested_reasons
  - 新規エントリの追加: event_key / anomaly_key を空にして送ると追加。event は event_date, name, duration_days が必須
  - 既存エントリの削除: payload から key を省略すると削除。validate_story の最小件数ルールが適用される
  - condition_json は JSON textarea に加え、place, expected_place, zone, time_from, time_to, min_tension, required_event, multiple_characters, alone, stress_threshold_min の簡易ビルダーで編集できる
  - runtime が即時判定する主な condition_json key は place, zone, time_from/time_to, alone, multiple_characters, stress_threshold_min である
  - 簡易ビルダーは未知の condition_json key を保持し、必要に応じて JSON textarea 側で直接編集できる
  - 保存時は condition_json の既知 key を server-side でも検証する。place / expected_place は既存 place_id、zone は有効 zone、time_from / time_to は存在する場合 HH:MM、alone / multiple_characters は boolean、min_tension / stress_threshold_min は 0.0〜1.0 の数値であること
  - place_id / key 変更は扱わない
  - `ankoku_gakuen` などの template story は read-only
  - runtime が `stopped` の story だけ保存できる
  - 保存時に `world_config.yaml` を更新し、`validate_story`, `update_story`, story metadata export を実行する
  - PUT の戻り値に `events_added`, `events_removed`, `anomalies_added`, `anomalies_removed` を含む
- `/admin/chapters/{story_id}`
  - chapter system の最小管理画面
  - active chapter / pending chapter / recent closed chapter と各 beat を表示する
  - director persona の一覧、active 表示、交代履歴を表示する
  - active director persona の切替を行う
  - chapter proposal の生成、承認、却下を行う
  - pending chapter を active chapter に切り替える
  - active chapter を手動 close し、closed chapter を active chapter として reopen できる
  - close / reopen では turn_number を必須入力とし、reopen は既存 active chapter がある場合 409 にする
  - active chapter の current_beat を既存 beat phase から選んで手動更新できる
  - chapter / beat / director persona 定義そのものの詳細編集はこの画面では扱わない
- `/admin/directors/{story_id}`
  - clone 後の custom story 向けの監督ペルソナ定義編集画面
  - 編集対象: director.yaml の persona_id, name, aesthetic, values, traits, default_active
  - persona の追加・削除は payload のフルリスト置換で扱う
  - active 切替の実運用と交代履歴確認は `/admin/chapters/{story_id}` で扱う
  - `ankoku_gakuen` などの template story は read-only
  - runtime が `stopped` の story だけ保存できる
- `/admin/chapter-definitions/{story_id}`
  - clone 後の custom story 向けの章定義編集画面
  - 編集対象: chapters.yaml の chapter_id, title, theme, world_injection, start_condition, beats
  - beats は phase, description, goal, events JSON array のカード UI で追加・削除・編集できる
  - beat events は type, desc, target_char_id, place_id, priority, extra JSON object の簡易カードで編集できる
  - events JSON array と event カードは beat 単位の同期ボタンで相互反映できる
  - 保存 API は beat events を object 配列として検証し、priority は整数のみ受け付ける
  - chapter の追加・削除は payload のフルリスト置換で扱う
  - active / closed chapter の runtime 履歴操作は `/admin/chapters/{story_id}` で扱う
  - `ankoku_gakuen` などの template story は read-only
  - runtime が `stopped` の story だけ保存できる
- `/admin/conversation-patterns/{story_id}`
  - story ごとの会話発生パターンを切り替える画面
  - 初期実装では組み込みの「会話輪舞」(`solo_seed_rondo`) のオン/オフ、強さ、クールダウンターンだけを編集する
  - 会話輪舞は「独り言を別キャラが拾い、元のキャラへ戻して小さな話の種として連鎖させる」弱い runtime 制御である
  - story YAML は編集せず、`conversation_motif_settings` に runtime 設定として保存する

対応 API:

- `GET /admin/api/v1/stories/{story_id}/viewer`
- `GET /admin/api/v1/stories/{story_id}/onboarding-template`
- `POST /admin/api/v1/stories/{story_id}/clone`
- `GET /admin/api/v1/stories/{story_id}/characters/edit-template`
- `PUT /admin/api/v1/stories/{story_id}/characters`
- `GET /admin/api/v1/stories/{story_id}/places/edit-template`
- `PUT /admin/api/v1/stories/{story_id}/places`
- `GET /admin/api/v1/stories/{story_id}/definition/edit-template`
- `PUT /admin/api/v1/stories/{story_id}/definition`
- `GET /admin/api/v1/stories/{story_id}/event-anomalies/edit-template`
- `PUT /admin/api/v1/stories/{story_id}/event-anomalies`
- `GET /admin/api/v1/stories/{story_id}/director-personas/edit-template`
- `PUT /admin/api/v1/stories/{story_id}/director-personas`
- `GET /admin/api/v1/stories/{story_id}/chapter-definitions/edit-template`
- `PUT /admin/api/v1/stories/{story_id}/chapter-definitions`
- `GET /admin/api/v1/stories/{story_id}/conversation-patterns`
- `PUT /admin/api/v1/stories/{story_id}/conversation-patterns`
- `GET /admin/api/v1/stories/{story_id}/chapters`
- `GET /admin/api/v1/stories/{story_id}/chapter-proposals`
- `POST /admin/api/v1/stories/{story_id}/chapter-proposals/generate`
- `PUT /admin/api/v1/stories/{story_id}/chapter-proposals/{proposal_id}/approve`
- `PUT /admin/api/v1/stories/{story_id}/chapter-proposals/{proposal_id}/reject`
- `PUT /admin/api/v1/stories/{story_id}/director-personas/{persona_id}/activate`
- `POST /admin/api/v1/stories/{story_id}/chapters/{chapter_db_id}/activate`
- `POST /admin/api/v1/stories/{story_id}/chapters/{chapter_db_id}/close`
- `POST /admin/api/v1/stories/{story_id}/chapters/{chapter_db_id}/reopen`
- `PUT /admin/api/v1/stories/{story_id}/chapters/{chapter_db_id}/current-beat`
- `GET /admin/api/v1/settings`
- `PUT /admin/api/v1/settings/archive-publish`
- `PUT /admin/api/v1/settings/llm-runtime`

`PUT .../characters` は multipart を受け取り、`payload` に JSON、任意で
`character_image__{char_id}` に `neutral.png`、または
`character_image__{char_id}__{expression}` に各表情 PNG を渡せる。JSON body のみの場合は画像更新なしで基本項目だけを更新する。
payload item の `new_char_id` が既存 `char_id` と異なる場合は character_id rename として扱い、PUT 戻り値に
`characters_added`, `characters_removed`, `characters_renamed` を含める。

## archive publish loop

- `story_publications.visibility = public` の story だけ publish 対象
- 全体設定 `archive_publish_interval_minutes` を分単位で読む
- page JSON と catalog JSON の生成先は `web/published`
- build 失敗時は publisher state に error を残し、service 自体は継続する

## deploy 契約

- 正本の route 契約
  - `/admin/*` → reverse proxy → `127.0.0.1:8787`
  - `/viewer.php`, `/api.php`, `/archive.php`, `/archive_index.php`, `/assets/*`, `/published/*` → PHP/static
- 参考配備ファイル
  - `deploy/systemd/pocketrole-admin.service`
  - `deploy/examples/pocketrole-admin.env.example`
  - `deploy/nginx/pocketrole.conf.example`

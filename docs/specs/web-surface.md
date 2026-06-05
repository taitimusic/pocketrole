# PocketRole Web 公開面仕様

更新日: 2026-03-18

## 目的

PHP viewer と公開 API の current behavior を整理し、DB ではなく JSONL が public surface の正本であることを明確にする。

## この文書の正本範囲

- `web/viewer.php`
- `web/map_replay.php`
- `web/api.php`
- `web/receiver.php`
- `web/status.php`
- `web/archive.php`
- `web/archive_index.php`
- `web/chatlog_lib.php`
- `web/config.php`

## 更新基準

公開 endpoint、認証要件、JSONL 保存形式、viewer / replay UI 構成が変わったら更新する。

## 関連資料

- [runtime-architecture.md](./runtime-architecture.md)
- [../WEB_API_VISIBILITY_PROPOSAL.md](../WEB_API_VISIBILITY_PROPOSAL.md)

## 現在の public state source

viewer の current state source は local SQLite ではなく、story ごとの JSONL `.dat` である。  
保存先は `DATA_DIR/<story_id>/*.dat` で、日付単位のファイル名を使う。

この構成のため、public viewer は engine process や DB に依存せず、`receiver.php` に送られた結果だけを表示する。

## `viewer.php`

### 役割

- story_id を受けて公開タイムラインページを返す
- page 自体は薄い shell で、データ取得と表示ロジックは主に `assets/app.js` 側にある

### 現在の UI

- ヘッダ
- story switcher
- poll status
- timeline
- `最新へ` ボタン
- `マップ再生を見る` CTA

現在の viewer は 2D scene UI ではなく、タイムライン表示が主である。

### UI locale

public viewer / map replay / archive の固定 UI 文言は `ja`, `en`, `zh-TW` に対応する。
locale は `lang` query、保存済み browser 設定、browser language、`ja` の順で解決する。
story title、キャラ名、場所名、ログ本文、ニュースタイトルは story/runtime データとして扱い、自動翻訳しない。

## `map_replay.php`

### 役割

- `story_id` を受けて、保存済みログを再生する別 viewer を返す
- データは `api.php?action=replay` から取得する
- place 画像がある story では `web/assets/story_maps/{story_id}/images/{place_id}_320.png` を優先する
- story map asset がある story では `web/assets/story_maps/{story_id}/map.json` と背景画像も利用できる
- `tools/import_story` / `tools/update_story` が `place_manifest.json` と `images/` scaffold を作るが、実画像と `map.json` は手動 asset として扱う

### 現在の UI

- Phaser ベースの replay viewer
- place-image mode と story-map mode を持つ
- `play / pause / next / reset / speed` の最小制御
- BGM は `play` 開始時の初回 user gesture で unlock し、loop 再生する
- BGM の解決順は `bgm_manifest.json` の `story_default` -> `assets/audio/pocketrole_bgm.mp3`
- live timeline viewer とは別 URL 面

### current cache note

- `map_replay.php` は `assets/map_replay.css`, `assets/phaser.min.js`, `assets/i18n.js`, `assets/map_replay.js` に `filemtime` ベースの cache-busting query を付ける
- shared hosting では `assets/map_replay.js` や place 画像に長めの cache-control が付くことがある
- その場合、server 側の `placeImage` 実装や asset を更新しても、ブラウザが古い JS を保持して `storyMap` 側の見え方に戻ったように見えることがある
- live で mode が期待どおりに切り替わらないときは、まず hard reload / private window / cache-busting query で確認する

## `archive_index.php` / `archive.php`

### 役割

- `archive_index.php` は公開済み story archive 一覧を返す
- `archive.php` は `story_id` と `page` を受けて公開済み本文をページ単位で読む

### current state source

- DB は読まない
- `web/published/catalog.json`
- `web/published/stories/<story_id>/manifest.json`
- `web/published/stories/<story_id>/pages/<page>.json`

### current behavior

- `draft` story は catalog に載らない
- viewer は manifest と要求 page だけを fetch し、全 story 本文は読まない
- `archive.php` は live timeline viewer とは別 URL 面である

## `api.php`

### 共通ルール

- GET のみ
- `story_id` は `[a-z0-9_]` に sanitize
- story directory が存在しない場合は 404

### `action=latest`

- 認証不要
- `limit <= 100`
- `collect_latest_entries()` で日次ファイルを逆順に見て、logical key 単位で最新勝ち集約する

返す主な項目:

- `logs`
- `count`
- `has_more`

### `action=replay`

- 認証不要
- `collect_latest_entries()` から直近 window を組み立てる
- `story`, `places`, `characters`, `events` をまとめて返す
- `map_replay.php` がそのまま消費できる replay payload を返す

### `action=history`

- 認証必須
- `from` / `to` は `YYYY-MM-DD`
- 対応 `.dat` を date range で集め、dedupe 後に返す

### `action=characters`

- 認証必須
- 最新 2 ファイルだけを見て、char ごとの最後の entry から current snapshot を作る

返す主な項目:

- `char_id`
- `current_place`
- `last_message`
- `last_update`
- `current_emotion`
- `current_expression`

ここでいう `current_emotion` は public log の `emotion_snapshot` 由来であり、DB の生 state を直接引くものではない。

### current 認証境界

- `latest` と `replay` は公開
- `history` と `characters` は保護
- 保護 endpoint は `web/config/auth_token.txt` を読む
- token file が欠如している場合は 500 を返す

## `receiver.php`

### 役割

- `WebPoster` からの POST を受け、story ごとの当日 `.dat` に upsert する

### current behavior

- POST のみ
- JSON body 必須
- `auth_token` 必須
- `story_id` sanitize
- `logs` が空なら 400
- story directory がなければ作る
- 同一 logical key は latest wins で上書きする
- `action=reset_story` と `confirm=true` の POST は、同じ token 境界で story directory 内の `*.dat` だけを削除する
- reset action は `logs` を要求しない。`index.html` や directory scaffold は残す
- token は `web/config/auth_token.txt` から読み、`.htaccess` により外部公開を拒否する前提で置く
- 一時デバッグ中は `web/config/receiver_debug.log` へ stage ログを出せる

### current compatibility note

- hosted shared environment を考慮し、`web/` では PHP 8 専用 API や 7.4 専用 arrow function への依存を避ける
- 直近 live 修正では `str_starts_with()` 依存と `fn(...)` を外し、`latest` / `receiver` の hosted 疎通を回復した

## `status.php`

### 役割

- 公開ヘルスチェック用 endpoint
- `DATA_DIR` 配下の story directory を列挙して、各 story の `last_received` と `log_count` を返す

### 注意点

- `log_count` は `.dat` の行数集計であり、dedupe 後の unique count ではない
- current viewer 側の観測値として便利だが、DB の `chat_logs` 件数とは別物である

## JSONL helper の仕様

`chatlog_lib.php` には current public log の基礎ルールがある。

### logical key

`sim_datetime(normalized) | char_id | turn_number`

同じ logical key を持つ entry は 1 件に潰され、後勝ちになる。

### datetime normalization

`normalize_sim_datetime()` は `Y-m-dTH:i` までに丸める。  
そのため秒違いの entry でも同一 key になりうる。

### `latest` 集約

- `.dat` を新しい日付から走査する
- 各ファイル内では末尾から見る
- logical key が未出なら採用する

## story map scaffold

- `web/assets/story_maps/{story_id}/place_manifest.json`
- `web/assets/story_maps/{story_id}/images/index.html`
- `web/assets/story_maps/{story_id}/bgm_manifest.json`
- `web/assets/story_maps/{story_id}/bgm/index.html`

これらは story lifecycle 側が自動生成する scaffold である。  
`place_manifest.json` には `place_id`, `label`, `expected_image` が入り、Place 背景画像を手動投入する際のガイドとして使う。  
`bgm_manifest.json` には `enabled`, `story_default`, `place_overrides`, `mood_overrides` が入り、v1 では `story_default` だけを `map_replay.php` が使う。

## current public limitations

- public viewer は DB を読まない
- public archive viewer も DB を読まない
- `latest` と `replay` だけが認証不要で、`history` と `characters` は protected
- `status.php` の件数は logical dedupe 後の表示件数と一致しない場合がある
- `receiver.php` は旧 `.dat` の自動削除をしないため、hosted 側のログ掃除は運用タスクとして残る
- `map_replay.php` の見え方は browser cache の影響を受けうるため、server 反映直後の見た目差分は code 差分ではなく stale asset が原因の場合がある

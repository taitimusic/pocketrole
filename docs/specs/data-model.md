# PocketRole データモデル仕様

更新日: 2026-03-31

## 目的

story 定義、runtime state、公開ログの境界を整理し、DB や YAML を変更するときの不変条件を明確にする。

## この文書の正本範囲

- `db/schema.sql`
- `db/migrations/*.sql`
- `tools/import_story.py`
- `tools/update_story.py`
- `stories/*/*.yaml`

## 更新基準

テーブル構成、YAML マッピング、story update ルールが変わったら更新する。

## 関連資料

- [system-overview.md](./system-overview.md)
- [story-lifecycle-and-tools.md](./story-lifecycle-and-tools.md)

## データソースの層

PocketRole には現在 3 つの主要データ層がある。

### 1. Story definition YAML

`stories/<story_id>/world_config.yaml` と `characters.yaml`。  
authoring 時の入力であり、runtime の主正本ではない。  
character growth 後も runtime は YAML を自動上書きせず、必要な反映は overlay export/apply に分離する。

### 2. Local SQLite DB

import 後の runtime 正本。  
story 定義の展開先であり、シミュレーション結果と再開情報を持つ。

### 3. Hosted JSONL `.dat`

public viewer の正本。  
SQLite の mirror ではなく、Web 配信用に push される別ストアである。

## YAML から DB へのマッピング

### `world_config.yaml`

主に次へ展開される。

- `story` -> `stories`
- `places` -> `places`
- `time_schedules` -> `time_schedules`
- `event_calendar` -> `event_calendar`
- `anomaly_rules` -> `anomaly_rules`

### `characters.yaml`

主に次へ展開される。

- `characters[]` -> `characters`
- 初期 emotion / favorite place -> `character_states` の初期行
- character id 一覧 -> `relationships` の初期組み合わせ

`characters[].personality.speech_examples` と `characters[].personality.never_say` は、現在 `characters.speech` JSON の `examples`, `never_say` へ射影される。

マッピング時に JSON 文字列化される列が多く、DB 側で完全に正規化されているわけではない。

## 主要テーブル

### `stories`

story の root table。  
特に重要なのは次の列である。

- `id`
- `season_start`
- `turn_minutes`
- `turn_interval_sec`
- `last_sim_time`
- `llm_provider`
- `llm_model`
- `is_active`

`last_sim_time` は engine 再開位置として使うため、進行中 story では重要な state である。
`llm_provider` / `llm_model` は現在も DB に残っているが、current runtime の provider/model 解決には使わない。通常の運用切替は `config/llm_runtime.local.yaml` だけで行う。

### `places`

場所定義。  
`(id, story_id)` 複合主キーで、`is_active` を持つ。  
`adjacent_places` は JSON 文字列でも dict でも扱われうる。

### `characters`

キャラ基本設定。  
speech, strengths, weaknesses, emotion_default, favorite_places, expressions_available などは JSON 列で保存される。  
`speech` は少なくとも `first_person`, `speech_style` を持ち、現在は必要に応じて `examples`, `never_say` も保持する。  
`image_path` は現在 `images/<char_id>/neutral.png` の固定パターンで入る。

### `character_states`

時系列 state table。  
1 発言ごとに 1 行追加され、最新 current place, expression, emotion, move reason を持つ。

### `relationships`

`(story_id, char_id_from, char_id_to)` が主キー。  
trust に加え、current schema では `affinity`, `tension`, `familiarity`, `last_event_turn`, `last_event_summary` を持つ。

### `relationship_modes`

relationship の脚本モードを持つ durable table。
current code では directed pair ごとに `mode_type`, `status`, `summary`, `confidence`, `intensity`, `source_pattern_id`, `source_episode_id`, `last_reinforced_turn` を持つ。
active unique は pair 単位ではなく `story_id + char_id_from + char_id_to + mode_type` で、同一 pair に cluster ごと最大 2 active mode が共存しうる。

### `chat_logs`

全文ログの中心テーブル。  
public 投稿前の queue も兼ねており、`posted_to_web` を持つ。

重要列:

- `sim_datetime`
- `turn_number`
- `char_id`
- `msg_type`
- `target_char_id`
- `place_id`
- `expression`
- `message`
- `conversation_session_id`
- `reply_to_log_id`
- `scene_id`
- `speaker_intent`
- `quality_score`
- `quality_flags`
- `state_effect_summary`
- `generation_attempt`
- `emotion_snapshot`
- `llm_provider`
- `llm_model`
- `posted_to_web`

### `conversation_sessions`

継続会話の active state。  
participant 群は JSON で保持し、`last_log_id`, `last_speaker_id`, `last_activity_sim_datetime`, `status` を持つ。  
current schema では `scene_id`, `session_kind`, `closure_reason` も保持できる。

### `memories`

短期記憶用テーブル。  
schema 上は存在するが、current runtime での活用は薄い。

### `story_memory`

story 単位の出来事要約。
scene summary や物語的変化の蓄積先で、`character_evolution` や archive / novel 系の材料になる。

### `character_evolution`

キャラ設定の overlay 変化履歴。
`field`, `previous_value`, `new_value`, `source_memory_id` を持つ。
current code では `source_canon_bit_id` も持ち、growth 由来だけでなく canon writeback 由来の変化監査にも使う。

### `story_scenes` / `scene_participants`

scene-aware runtime の durable state。

- `story_scenes`
  - place ごとの active/closed scene、`scene_type`, `status`, `place_id`, `opened_turn`, `closed_turn`, `outcome_summary` などを持つ
- `scene_participants`
  - scene ごとの参加者、`role`, `speak_budget`, `times_spoken`, `last_spoken_turn` などを持つ

### `story_hooks`

会話から抽出した未解決課題の durable table。  
question / promise / conflict などの hook を `status`, `priority`, `source_scene_id`, `source_canon_bit_id`, `resolution_*` 付きで保持する。

### `relationship_events`

`relationships` の変化理由を append-only で残す table。  
`delta_trust`, `delta_affinity`, `delta_tension`, `summary`, `source_log_id`, `scene_id`, `turn_number` を持つ。

### `generation_quality_issues`

本文品質の検査結果。  
過長、反復、抽象語ループ、装飾違反、growth quality rejection などを `issue_type`, `severity`, `details`, `auto_action`, `created_turn` で記録する。
current runtime では `auto_action` に `normalize`, `shorten`, `retry`, `fallback`, `skip`, `rollback` が現れうる。

### `character_profile_overlays` / `character_growth_candidates`

runtime 成長系の durable state。

- `character_profile_overlays`
  - seed YAML に対する現在有効な overlay を `overlay_json` と version 付きで持つ
- `character_growth_candidates`
  - 未 commit の候補変化を `field`, `candidate_value`, `experience_score`, `identity_impact_score`, `confidence`, `status` 付きで持つ

### `character_canon_overlays`

canon writeback 用の current-state overlay。
`story_canon_bits` から昇格した stable motif を、prompt 用に `current_goal`, `current_worry`, `personality_core` へ慎重に反映する。
`character_profile_overlays` へは直接混ぜず、current code では separate layer として保持し、prompt 時に `growth overlay > canon overlay > base character` の順で merge する。

### `narrative_tensions` / `director_interventions`

story director 系の状態。
current runtime では tension 検出と intervention 適用の持続データとして使う。

### `story_interaction_patterns`

deterministic pattern layer の durable state。
`pattern_type`, `status`, `involved_chars`, `dedupe_key`, source hook/tension/scene/event, `recurrence_count`, `intensity`, `confidence` を持つ。
current runtime では `misunderstanding`, `status_clash`, `bluff_or_showoff`, `near_reveal`, `role_reversal`, `small_win_loss` を persisted state として保持する。

### `conversation_motif_settings` / `conversation_motif_runs`

会話発生パターン（会話輪舞モード）の durable state。

- `conversation_motif_settings`
  - story ごとの組み込み motif 設定を `motif_id`, `enabled`, `strength`, `cooldown_turns` で持つ
  - current implementation の組み込み motif は `solo_seed_rondo`
- `conversation_motif_runs`
  - `solo_seed` hook から始まった進行中の会話輪舞を `stage`, `owner_char_id`, `pickup_char_id`, `place_id`, seed hook/log で持つ
  - runtime は台詞内容ではなく、次の話者・相手・話題の受け渡しだけを弱く補助する

### `story_episodes`

episode planner の durable state。
current 実装では 1 story あたり active は最大 1 件で、`episode_type`, `goal`, `stakes`, `active_pattern_id`, `focus_char_ids`, `carry_over_hook_ids`, `focus_place_id`, `opened_turn`, `last_progress_turn`, `closed_turn`, `exit_condition`, `summary` を持つ。

### `story_canon_bits`

canon ladder の durable state。
current code では `last_reignited_turn` / `reignition_count` に加えて、`last_writeback_turn` / `writeback_count` を持ち、hook 再発火と profile writeback の両方の観測点になる。

### `story_dramatic_pressures`

pattern / relationship mode / canon / episode を上位解釈した dramatic pressure の durable table。
current code では `pressure_type`, `status`, `focus_char_ids`, `focus_place_id`, `dedupe_key`, source hook/tension/pattern/episode/relationship_mode/canon_bit/scene, `score`, `urgency`, `payoff_ready`, `intent_alignment` を持つ。
family は少なくとも `status_flashpoint`, `near_reveal`, `payoff_ready`, `stall_risk`, `showoff_flashpoint`, `role_reversal_ready` を含む。

### `story_arc` / `novel_output`

公開 archive や novel export の材料。
public timeline の正本ではなく、story を章立てや本文へ再構成する層である。  
current schema の `story_arc` には `source_scene_id` があり、scene-close prose の重複生成防止に使う。
また current 実装では `arc_type='episode'` が追加利用され、`episode_close` artifact の保存先になる。

### `ambient_states`

短期環境コンテキストの durable table。
scope 単位で ambient 要因を保持し、turn ごとに有効期限と感情差分を持つ。

### hosted/admin 系テーブル

次の管理・公開 metadata table がある。

- `admin_users`
- `admin_sessions`
- `admin_api_tokens`
- `story_publications`
- `system_settings`
- `admin_audit_logs`

## story 単位の不変条件

- ほぼ全テーブルは `story_id` で束ねられる
- `update_story` 中も `story.id` は変更不可
- 進行がある story では `season_start` と `turn_minutes` は変更不可
- `llm_provider` / `llm_model` 列は互換のため残るが、current runtime では切替元として参照しない
- 日常運用で実際に使う provider/model は `config/llm_runtime.local.yaml` の `active_profile` / `story_overrides` で決まる
- scene/hook/growth/director 系 state は code 上は実装済みだが、runtime config で明示有効化しないと更新されないことがある

## import と update の違い

### `import_story`

- story を新規構築する
- 既存 story がある場合は `--force-replace` がない限り失敗する
- 定義テーブルを丸ごと再投入する前提で、進行保持の安全策はない

### `update_story`

- 進行中 story の定義を安全側で更新するための CLI
- `season_start` と `turn_minutes` は、進行済み story では変更禁止
- places / characters は upsert と deactivate の組み合わせで更新する
- current state や chat log を壊さずに定義側を追従させるのが主目的

## migration と初期化

`DatabaseManager` は接続ごとに次を行う。

- WAL を有効化
- foreign key を有効化
- busy timeout を設定
- `db/migrations/*.sql` を version 順に適用

既知の migration 記録漏れについては、schema の存在状態から `schema_version` を補完する repair path がある。

## 公開ログとの関係

`chat_logs` と hosted `.dat` は同じではない。  
`chat_logs` の全列が hosted 側へ送られるわけではなく、`WebPoster._build_payload()` で public 用に射影される。

public payload へ行く主な列は次の通り。

- `sim_datetime`
- `turn_number`
- `char_id`
- `msg_type`
- `target_char_id`
- `place_id`
- `expression`
- `message`
- `emotion_snapshot`

`llm_provider`, `llm_model`, `conversation_session_id`, `posted_to_web` などは hosted JSONL には出ない。

## current implementation で壊しやすい点

- `posted_to_web` は public 配信キューとして使っているため、意味を変えると Web 反映が壊れる
- `last_sim_time` は再開位置なので、単純な metadata ではない
- `adjacent_places`, `participant_ids` などの JSON 列は Python/PHP の双方で解釈される
- `character_profile_overlays` は YAML の自動上書きではなく DB overlay 正本として扱う
- hosted viewer の正本は DB ではなく `.dat` なので、DB だけ変更しても public 表示は変わらない
- `story_publications` や `novel_output` は public archive の build 材料だが、`viewer.php` / `map_replay.php` の live surface とは別系統である

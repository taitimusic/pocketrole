# PocketRole ストーリー運用フローと CLI 仕様

更新日: 2026-04-08

## 目的

story authoring から import, update, smoke, export までの current workflow を整理し、どの tool が安全か、どこが破壊的かを明確にする。

## この文書の正本範囲

- `tools/validate_story.py`
- `tools/import_story.py`
- `tools/update_story.py`
- `tools/init_web_post_targets.py`
- `tools/generate_story_map_scaffold.py`
- `tools/smoke_engine_start.py`
- `tools/smoke_openai_models.py`
- `tools/monitor_engine_runtime.py`
- `tools/runtime_probe_story_events.py`
- `tools/export_character_overlays.py`
- `tools/apply_character_overlays.py`
- `tools/generate_story_review_draft.py`
- `tools/export_log.py`
- `tools/check_stability.py`
- `tools/activate_chapter.py`

## 更新基準

CLI の責務、破壊性、安全な運用手順が変わったら更新する。

## 関連資料

- [data-model.md](./data-model.md)
- [../users_manual/USER_MANUAL.md](../users_manual/USER_MANUAL.md)
- [../BUG_FIX.md](../BUG_FIX.md)

## current 推奨フロー

1. `stories/<story_id>/` に YAML を置く
2. `validate_story` で schema と参照整合を確認する
3. 新規 story なら `import_story`
4. 進行済み story の定義変更なら `update_story`
5. Web 投稿を使うなら `init_web_post_targets` で story ごとの投稿設定ファイルを作る
6. LLM の日常運用を切り替えるなら `config/llm_runtime.local.yaml` を編集する
7. engine を起動する
8. 小さく確認したいときは `smoke_engine_start`
9. OpenAI の model 互換確認には `smoke_openai_models`
10. 長時間運転は `monitor_engine_runtime` で外部監視する
11. review 下書きは `generate_story_review_draft`
12. manual chapter を使うなら `activate_chapter`
13. 成長結果の YAML 反映が必要なら `export_character_overlays` / `apply_character_overlays`
14. 過去ログ確認や共有には `export_log`

## `validate_story`

### 役割

- YAML の構文と最低限の story schema を検証する
- place id, day_type, zone, date/time format, provider 名などをチェックする

### current limitation

- semantic な物語品質は判定しない
- runtime でしか分からない不整合までは検出しない

## `import_story`

### 役割

- story YAML を DB へ新規投入する
- DB 初期化と migration 適用も含む

### 特徴

- 既存 story があると失敗する
- `--force-replace` を付けると再構築前提になる
- 初期 `character_states` も作る
- `web/assets/story_maps/{story_id}/` の scaffold も作る

### 破壊性

高い。  
進行中 story に対して安易に使う tool ではない。

### current reset 用途

意図的に story をクリーン状態へ戻したい場合は、`import_story --force-replace` が current reset 手段である。
この操作では `chat_logs`, `character_states`, `conversation_sessions`, story director 系 state なども story 単位で再構築される。

## `update_story`

### 役割

- 進行中 story の定義を、進行を保ったまま更新する

### current safety rule

進行済み story では次が guarded field になっている。

- `season_start`
- `turn_minutes`

これらを変えたい場合は `import_story --force-replace` 相当の再構築が必要になる。

### current update behavior

- places は upsert + deactivate
- characters も upsert + deactivate
- `characters.yaml` の `personality.speech_examples` と `personality.never_say` も `characters.speech` JSON へ反映される
- schedules / events / anomalies は story scope で入れ替える
- story map scaffold も current place 定義に同期する

## `init_web_post_targets`

### 役割

- `config/web_post_targets.local.yaml` を初期化し、story ごとの投稿先設定の空エントリを作る

### current behavior

- 既存ファイルがあれば merge し、既存値は上書きしない
- `enabled`, `receiver_url`, `auth_token` を story 単位で持つ
- 実ファイルは gitignore 対象で、repo には example だけを置く

## `generate_story_map_scaffold`

### 役割

- `stories/<story_id>/` の place 定義から `web/assets/story_maps/{story_id}/` の scaffold を backfill 生成する

### current behavior

- `place_manifest.json` を生成する
- `images/`, `bgm/` と `index.html` 群を作る
- `bgm_manifest.json` を、未作成時だけ scaffold として生成する
- `map.json` や実画像は自動生成しない
- 実 MP3 は自動生成しない

## `config/llm_runtime.local.yaml`

### 役割

- Ollama / OpenAI などの運用切替を 1 ファイルで行う
- `active_profile` で全 story の既定 runtime provider/model を決める
- `story_overrides` で特定 story だけ別 profile に差し替える

### current behavior

- repo には `config/llm_runtime.example.yaml` を置く
- 実ファイル `config/llm_runtime.local.yaml` は gitignore 対象
- runtime file が日常運用の唯一の provider/model 切替点
- `active_profile` または `story_overrides` で解決できない story は起動前 validation で失敗する
- `engine.main`, hosted admin runtime, `smoke_engine_start` は同じ resolver を使う

## `config.yaml` の story-emergence flags

scene-aware runtime は `config.yaml` 側の feature flag で opt-in する。  
`config.yaml.example` の既定値は多くが `enabled=false` であり、ファイルをコピーしただけでは新経路は有効にならない。

主なセクション:

- `scene_management`
- `participation_planner`
- `story_hooks`
- `character_drives`
- `relationship_dynamics`
- `quality_guard`
- `growth_engine`
- `episode_planner`

## `smoke_engine_start`

### 役割

- 指定 story を `/tmp` にコピーした作業 DB で数ターンだけ回す
- 本番 DB を汚さずに LLM と turn loop の疎通を確認する

### current limitation

- hosted Web 投稿はしない
- `config/web_post_targets.local.yaml` の投稿設定も使わない
- `config/llm_runtime.local.yaml` による runtime provider/model 切替は使う
- public viewer 更新確認には使えない

### OpenAI 関連の current note

- `.env` は shell 互換 parser ではないため、引用符や行末コメントを入れない
- OpenAI の model 一覧が `/v1/models` に見えていても、生成 endpoint 互換は別に確認が必要
- GPT-5 系は current implementation では `Responses API` へ自動で切り替わる
- `gpt-5-mini` は current implementation で `temperature` を送らず、`reasoning_mode=auto` でも `minimal` 寄りに補正する
- `gpt-5.4-mini` と `gpt-5.4-nano` は `Responses API + temperature` の smoke 成功を確認済み
- OpenAI/Ollama の日常切替は `config.yaml` や story YAML ではなく `config/llm_runtime.local.yaml` を編集するのが current 運用

## `smoke_openai_models`

### 役割

- OpenAI の visible model 一覧を取得し、指定候補との積集合だけを `/tmp` DB で順に smoke 実行する
- 各モデルの成功/失敗、使用 endpoint、最終出力の要約を JSON で返す

### current behavior

- `config.yaml` の一時コピーを作り、`providers.openai.default_model='<candidate>'`, `providers.openai.api_mode=auto` を設定して使う
- `llm_runtime.local.yaml` 相当の一時ファイルも作り、その run だけ candidate model を active profile として強制する
- 実 DB や hosted Web 投稿には影響しない
- 候補未指定時は `gpt-5-mini`, `gpt-5.4-mini`, `gpt-4.1-mini`, `gpt-4o-mini` を比較対象にする
- `gpt-5.4-nano` のような追加候補は `--models gpt-5.4-nano` のように明示指定して比較する

## `monitor_engine_runtime`

### 役割

- 実 DB の engine を外部監視し、Markdown へ health / progress / story quality を追記する
- `reply_ratio`, `all_chars_spoke_ratio`, `open_hooks`, `active_tensions`, `growth_commits_recent`, `scene_arcs_recent` などを継続観測する

### current behavior

- `docs/monitoring/<story_id>_runtime_monitor.md` を既定出力先にする
- warning 集合が変わったときも snapshot を追記する
- engine stop, stall, Ollama failure などの warning を deterministic に判定する
- current metrics には `sessionless_monologue_ratio`, `active_interaction_patterns`, `active_relationship_modes`, `complementary_mode_pairs_active`, `single_mode_pairs_active`, `relationship_mode_conflict_attention_pairs`, `complementary_mode_reinforcements_recent`, `complementary_mode_decay_updates_recent`, `complementary_mode_deactivations_recent`, `active_canon_bits`, `active_dramatic_pressures`, `active_episode_type`, `active_episode_age`, `active_chapter_id`, `active_chapter_beat`, `active_chapter_age`, `active_director_persona_id`, `director_satisfaction_overall`, `director_satisfaction_tension`, `director_satisfaction_pacing`, `director_satisfaction_surprise`, `episode_close_completion_rate`, `canon_reignitions_recent`, `canon_writebacks_recent`, `quality_output_normalized`, `quality_output_fallback`, `reply_quality_normalizations_recent`, `reply_quality_fallbacks_recent`, `reply_focus_misses_recent`, `generic_reply_tails_recent`, `voice_flat_replies_recent`, `growth_empty_recent`, `growth_quality_rejections_recent` も含まれる
- current warning には `reply_quality_fallback_high`, `reply_quality_flat`, `scene_close_weak`, `relationship_mode_flat`, `chapter_progress_stalled`, `director_satisfaction_low` も含まれる

## `generate_story_review_draft`

### 役割

- 実 DB と monitor Markdown から `docs/system_review/generated/` 向け review draft を生成する

### current behavior

- canonical review 台帳は自動追記しない
- `Intent Lens / Pattern Lens / Relationship Mode Lens / Canon Lens / Pressure Lens / Episode Lens / Data Snapshot / What Worked / Gaps / Cause Lens / Recommendations / Recommended Experiments / Next Checkpoints` を sidecar Markdown に出す
- `Cause Lens` は gap の列挙を上位原因へ束ね、`Why / Evidence / Next experiment` を最大 3 件まで出す
- monitor file が無い場合は DB-only で続行する
- current review では `scene_close_weak`, `reply_quality_flat`, `chapter_progress_stalled`, `director_satisfaction_low`, `director_pacing_sat_low`, `director_surprise_sat_low` を gap として扱う
- current `Cause Lens` では `quality_gate_suppression`, `chapter_pressure_not_converting`, `director_alignment_low` を quality/chapter slice の主要 cause として使う

## `runtime_probe_story_events`

### 役割

- `/tmp` の DB copy 上で story を round 単位に進める
- `scene_close`, `growth`, `pattern`, `episode` の handoff を deterministic に観測する

### current behavior

- `goal=round_end | scene_close | growth` を持つ
- `scene_arc_delta`, `novel_output_delta`, `growth_candidate_delta`, `growth_commit_delta` を JSON で返す
- current 実装では `pattern_delta`, `relationship_mode_delta`, `canon_delta`, `canon_promotions_delta`, `canon_reignitions_delta`, `pressure_delta`, `episode_delta`, `active_pattern_count`, `active_relationship_mode_count`, `active_canon_bit_count`, `active_dramatic_pressure_count`, `active_episode_type`, `dominant_pattern_types`, `dominant_canon_motifs`, `dominant_pressure_types` も返す
- `smoke_engine_start` と違い、post-round artifact の truth-finding 用に使う

## current tool の使い分け

- `smoke_engine_start`
  - `/tmp` DB copy で turn loop と LLM 疎通を見る
- `runtime_probe_story_events`
  - post-round artifact の deterministic truth-finding を行う
- real DB + `monitor_engine_runtime` + `generate_story_review_draft`
  - visible quality, quality gate suppression, growth commit, Web 投稿前の保存状況を観測する

## `activate_chapter`

### 役割

- `start_condition: manual` の pending chapter を CLI から active にする

### current behavior

- `story_id + chapter_id + turn` を受け取る
- 既に active chapter がある場合は拒否する
- target chapter が pending でない場合は拒否する
- `import_chapters` 後の最小運用では、この CLI が chapter 開始手段になる

## `export_character_overlays` / `apply_character_overlays`

### 役割

- runtime growth で溜まった `character_profile_overlays` を YAML へ書き出す
- 明示操作で `characters.yaml` に apply する

### current safety rule

- runtime は YAML を自動更新しない
- apply は registry に載った field だけ canonical YAML へ反映する
- `--in-place` を使わない限り非破壊出力である

## `export_log`

### 役割

- `chat_logs` を markdown または text へ整形して出力する

### 特徴

- story 単位
- `provider` / `model` filter あり
- stdout も file 出力も可能

## `check_stability`

### 役割

- DB や story の安定性確認に使う補助 CLI
- `--v2-closeout` を付けると Chapter / Director Persona / scene close / reply residual をまとめて warning 化する

### 備考

通常は import / update 後の簡易 sanity check に使う。`upgrade-v2` を閉じる段階では、
`--v2-closeout --window-turns 40` を使って `chapter_progress_stalled`,
`director_satisfaction_low`, `scene_close_weak`, `reply_quality_residual` を先に確認する。
標準手順は [../runbooks/upgrade-v2-closeout.md](../runbooks/upgrade-v2-closeout.md) を参照。

## current 運用上の判断

### `characters.yaml` の current speech 拡張

`characters.yaml` の `personality` では、通常の `speech_style` に加えて次も持てる。

- `speech_examples`
- `never_say`

これらは `import_story` / `update_story` で DB の `characters.speech` JSON へ保存され、prompt 生成時の話し方誘導に使われる。
未指定 story では空配列扱いで、後方互換を壊さない。

### 新規 story

- `validate_story`
- `import_story`
- `init_web_post_targets`（Web 投稿を使う場合）
- `smoke_engine_start`
- 本起動

### 進行済み story の定義変更

- `validate_story`
- `update_story`
- 必要なら `generate_story_map_scaffold` で既存 story asset scaffold を backfill
- 必要なら `smoke_engine_start`
- 本起動継続

### viewer 反映確認

- `smoke_engine_start` ではなく、実 DB + `WebPoster` を通る実 run で見る
- 判定には `status.php` と `api.php?action=latest` を使う

### story-emergence review

- 実運転レビューでは `monitor_engine_runtime` と `generate_story_review_draft` を併用する
- ただし new runtime flag が無効のままだと、code 上の scene/hook/growth 機能があっても review 上は旧来の全員発話パターンが出る

## AI 実装時の注意点

- `import_story` と `update_story` の役割を混同しない
- `/tmp` smoke DB での成功を、そのまま hosted viewer 成功だとみなさない
- `stories.llm_model` を一時変更した検証では、終了後に元モデルへ戻す

# PocketRole ランタイム構造

更新日: 2026-04-10

## 目的

engine 起動から Web 投稿までのランタイム構造を整理し、task 分割、共有資源、停止処理、失敗時挙動を把握できるようにする。

## この文書の正本範囲

- `engine/main.py`
- `engine/process_manager.py`
- `engine/web_poster.py`
- `engine/llm/router.py`
- `db/db_manager.py`

## 更新基準

起動順序、並行実行モデル、shared object、公開反映経路が変わったら更新する。

## 関連資料

- [system-overview.md](./system-overview.md)
- [simulation-engine.md](./simulation-engine.md)
- [web-surface.md](./web-surface.md)

## 起動シーケンス

`engine.main` の current startup は次の順で進む。

1. `config.yaml`, `.env`, `config/web_post_targets.local.yaml`, `config/llm_runtime.local.yaml` を読み込む
2. JSON logger を設定する
3. `DatabaseManager` を開き、PRAGMA 設定と migration を適用する
4. 起動対象 story に対して `config/llm_runtime.local.yaml` の `active_profile` / `story_overrides` から runtime provider/model を解決する
5. 実際に必要な provider だけで `LLMRouter` を生成し、health check を走らせる
6. story ごとの Web 投稿設定を解決し、`enabled=true` かつ設定完備の story だけ `WebPoster` を生成して background task として起動する
7. `ProcessManager` を生成し、story ごとに `StoryEngine` task を起動する
8. シグナル受信時は `ProcessManager.stop_all()` と `WebPoster.stop()` を呼ぶ

OpenAI 系の current behavior は次のとおり。

- `.env` は shell 互換ではなく単純な `KEY=VALUE` parser で読む
- `providers.openai.api_mode` は `auto | chat_completions | responses`
- `api_mode=auto` では `gpt-5*` 系は `Responses API`、それ以外は `Chat Completions` を使う
- cloud API key は `load_config()` が `.env` / process env から取り込み、`LLMRouter` は `os.environ` ではなく Config 内の `api_key` を使って client を生成する
- `gpt-5-mini` 系では current implementation が `temperature` を送らず、`reasoning_mode=auto` でも `minimal` 寄りに補正して incomplete response を避ける
- `gpt-5.4-mini` / `gpt-5.4-nano` は current implementation で `Responses API + temperature` の smoke 成功を確認済み

LLM 運用切替の current behavior は次のとおり。

- 日常運用で切り替える正本は `config/llm_runtime.local.yaml`
- `active_profile` が全 story の既定 runtime provider/model を決める
- `story_overrides.<story_id>` があれば、その story だけ profile を差し替える
- runtime file に provider/model が無い story は起動前 validation で失敗する
- そのため、通常運用では story YAML や `config.yaml` を編集せずに Ollama / OpenAI を切り替えられる

hosted 配備では別系統として `admin.main` が control plane になる。

1. 管理者認証情報と hosted 設定を env/CLI から解決する
2. `DatabaseManager` と migration を初期化する
3. 起動対象 story に必要な provider だけで `LLMRouter` を共有オブジェクトとして起動する
4. `HostedRuntimeController` が story ごとの `StoryEngine` を束ね、`WebPoster` は story 開始時に lazy に解決・起動する
5. admin runtime でも `config/llm_runtime.local.yaml` の story 別解決をそのまま使う
6. background で archive publish loop を走らせる
7. `/admin/*` は `aiohttp` が担当し、public PHP surface とは trust boundary を分ける

## 共有オブジェクト

### 全 story で共有されるもの

- `DatabaseManager`
  - 1 process あたり 1 connection を共有する
  - `aiosqlite` 接続が timeout した場合は `sync_sqlite` backend に切り替わる
- `LLMRouter`
  - provider client と concurrency 制御を共有する
- hosted admin の publisher state
  - `healthz` と運用確認用の軽量状態を共有する

### story ごとに分かれるもの

- `StoryEngine`
- `WebPoster`
- hosted 側 chatlog directory

## 並行実行モデル

### StoryEngine

各 story は 1 本の `asyncio.Task` で走る。  
`StoryEngine.run()` には current code で 2 系統ある。

- legacy path
  - その story の全キャラを 1 周 `run_one_turn()` する
- scene-aware path
  - round 開始時に `story_scenes` / `scene_participants` を同期する
  - `ParticipationPlanner` が speaker queue を作り、その queue だけ `run_one_turn()` する
- どちらの path でも、1 周終わったら `turn_interval_sec` だけ sleep する

scene-aware path は次の feature flag が前提で、未設定または `enabled=false` では legacy path のままになる。

- `scene_management`
- `participation_planner`

### LLMRouter

provider ごとの concurrency policy は `LLMRouter` に閉じている。

- `ollama`
  - `supports_concurrent=False`
  - `asyncio.Queue(maxsize=1)` で直列化する
- cloud provider
  - `supports_concurrent=True`
  - `asyncio.Semaphore(cloud_concurrency)` で制限する

OpenAI provider の endpoint 切替は provider client 内で閉じている。

- `gpt-5`, `gpt-5-mini`, `gpt-5.4`, `gpt-5.4-mini` などは `Responses API`
- `gpt-5.4-nano` も `Responses API`
- `gpt-4.1-mini`, `gpt-4o-mini` などは `Chat Completions`
- 401/403/404/429 では OpenAI の error body (`message`, `type`, `code`) を失わずに例外へ載せる

このため、複数 story を同時実行しても Ollama 呼び出しは engine process 内で 1 本ずつになる。  
また、runtime file で OpenAI profile を有効にしている run では、未使用 provider の API key や health check を気にせず起動できる。

### WebPoster

story ごとに 1 task があり、`posted_to_web=0` の `chat_logs` をバッチ送信する。  
送信先失敗が連続すると一時停止状態に入り、pause 経過後に自動復帰する。
送信先 URL と token は story 単位設定から引く。

- `engine.main`
  - 起動前に対象 story 全体を解決し、未設定 story は startup validation で止まる
- hosted admin runtime
  - story 開始時にその story だけ解決し、未設定 story は当該 start/restart だけ失敗する

## 1 発言生成までの流れ

1. `StoryEngine.run_one_turn()` が現在キャラを決定する
2. state / place / anomaly / schedule / conversation context を組み立てる
3. feature flag 有効時は active scene, speaker intent, relevant hooks, relationship summary, intervention texts, effective profile, relationship mode, canon bit, dramatic pressure, dominant signal, scene objective を prompt context に追加する
4. prompt を構築する
5. `LLMRouter.generate()` で provider client を呼ぶ
6. 保存対象本文を sanitize し、必要なら empty-final recovery を 1 回だけ試す
7. `quality_guard.enabled=true` の場合は sanitize 済み本文を `normalize -> hard-evaluate -> retry -> fallback` の順で処理する
8. 通過した本文だけ `chat_logs` と `character_states` に保存する
9. `story_hooks.enabled` / `relationship_dynamics.enabled` の場合は、保存直後に hook 抽出と関係更新を行う
10. 保存された `chat_logs` は `posted_to_web=0` で残る

post-round では、flag と round 条件が合えば次が順に走る。

1. `story_memory`
2. `narration`
3. `growth_engine` または legacy `character_evolution`
4. closed scene の hook consolidation
5. `interaction pattern processing`
6. `relationship mode processing`
7. `emergent canonizer processing`
8. `canon reignition processing`
9. `canon profile writeback`
10. `dramatic pressure processing`
11. `chapter manager processing`
12. `episode planner processing`
13. `StoryDirector`
   - active tension の deterministic escalation
   - open hook / recent closed conversation scene / recent relationship event からの deterministic tension seeding
   - active interaction pattern / relationship mode / canon / dramatic pressure / active episode を見た tension / intervention ranking
   - LLM analysis による tension update / merge / intervention 生成
   - 必要時の fallback intervention
14. `NovelGenerator`
   - `scene_close` prose
   - `episode_close` prose

reply path の current runtime では `focus -> signal visibility -> dramatic move -> shape/blandness -> surface/variety` の順で contract を組み立てる。
keep-path に残せる reply でも `reply dramatic contract` を満たさず soft landing だけで終わる場合は、
`voice_flat_reply.details` に `reply_dramatic_move_mode`, `reply_dramatic_move_seen`,
`reply_soft_landing_used`, `reply_shape_reused_recently` を残して retry / normalize を切り分ける。
current 実装ではここに `reply_dramatic_semantic_family`, `reply_dramatic_anchor_seen`,
`reply_dramatic_semantic_missed` も含め、token が合っていても pressure anchor を持たない 2文目は keep しない。
加えて `reply blandness contract` が `same shape` の再利用と `pressure shift` 不足を拾い、
`blandness_contract_missed` が立つ reply は keep-path に残さない。
さらに `reply shape contract` が `story_pressure_tokens` を使って 2文目に scene pressure を残せているかを見て、
`story_flavor_weak` が立つ reply は `reply_quality_flat` の内訳として monitor / review で追う。
さらに `reply quality contract` が `same shape + repeated second beat` と `story_flavor_weak` をまとめて見て、
`reply_quality_contract_missed` が立つ reply は keep-path に残さない。
さらに `reply story quality contract` が `story_pressure_cue` と quality contract を束ね、
`reply_story_quality_contract_missed` が立つ reply は keep-path に残さない。
さらに `reply residual quality contract` が keep-path 最終段で
`same shape + repeated second beat + no pressure shift` と `soft landing + no story flavor` を再確認し、
`reply_residual_contract_missed` が立つ reply も keep-path に残さない。
monitor / review では `reply_bland_shape_reused_recent`, `reply_pressure_shift_missing_recent`,
`reply_story_flavor_weak_recent`, `reply_second_beat_reused_recent`,
`reply_quality_keep_blocked_recent`, `reply_story_quality_keep_blocked_recent`,
`reply_residual_keep_blocked_recent`,
`reply_bland_keep_blocked_recent`, `reply_reused_second_beat_recent`
でこの残差を観測する。
加えて keep-path は `variety_contract_missed` に `recent_second_beat_reused=true` が重なる reply を残さず、
opening / ending だけでなく second beat の反復も block 条件として扱う。

`scene_close` handoff は current code で same-round close だけを処理対象にしない。
post-round の `StoryEngine` は newly closed scene を最優先しつつ、直近 closed conversation scene の backlog も再確認し、
`missing_arc` を先、`missing_novel_output` を後に oldest-first で補完する。
scene-close context は `_closed_story_scene_ids` だけに依存せず、DB から scene / recent logs / hooks / tensions を引き直して再構築する。
current 実装では recent logs が空でも `scene_participants` を fallback に使って participant 名を復元し、
`scene_outcome_summary` と合わせて `missing_arc` handoff 自体は止めない。
`missing_novel_output` 補完では existing scene arc を再利用し、prose だけを追加して duplicate arc は作らない。
existing arc がある scene-close では、その summary を prose handoff の first fallback とし、
parse failure 時も existing arc title / summary を優先して prose-only completion を継続する。

current code で存在する主な opt-in feature flag は次の通り。

- `scene_management`
- `participation_planner`
- `story_hooks`
- `character_drives`
- `relationship_dynamics`
- `quality_guard`
- `growth_engine`
- `episode_planner`

## 1 発言の public 反映までの流れ

1. `WebPoster` が `get_unposted_logs()` で未送信ログを取る
2. story ごとの `receiver_url` / `auth_token` を使って JSON payload に変換する
3. hosted `receiver.php` へ POST する
4. hosted 側で story 日次 `.dat` へ upsert する
5. 成功したら local `mark_logs_posted()` で `posted_to_web=1` にする
6. `viewer.php` / `api.php` が `.dat` を読む

## ログと観測性

現在の標準ログは JSON 形式で、少なくとも次を持てる。

- `story_id`, `char_id`, `turn`
- `llm_provider`, `llm_model`
- `llm_latency_ms`
- `llm_response_chars`, `llm_thinking_chars`
- `llm_done_reason`, `llm_completion_status`
- `llm_recovery_attempted`

thinking / recovery 系の診断は、このログ項目を主な根拠としている。

外部観測用には `tools.monitor_engine_runtime` があり、実 DB と process 状態から次の story-quality 指標を Markdown 化できる。

- `speaking_chars_per_turn_avg`
- `all_chars_spoke_ratio`
- `reply_ratio`, `sessionless_monologue_ratio`
- `open_hooks`, `active_tensions`, `live_interventions`
- `active_interaction_patterns`, `active_relationship_modes`, `active_canon_bits`, `active_dramatic_pressures`
- `dominant_pattern_types`, `dominant_relationship_modes`, `dominant_pressure_types`
- `active_episode_type`, `active_episode_age`, `episode_close_completion_rate`
- `active_chapter_id`, `active_chapter_beat`, `active_chapter_age`
- `active_director_persona_id`, `director_satisfaction_overall`, `director_satisfaction_tension`, `director_satisfaction_pacing`, `director_satisfaction_surprise`
- `reply_dramatic_move_missing_recent`, `reply_soft_landing_recent`, `reply_shape_dominance_recent`
- `canon_reignitions_recent`, `canon_writebacks_recent`
- `growth_candidates_pending`, `growth_commits_recent`, `growth_empty_recent`, `growth_quality_rejections_recent`
- `quality_normalizations_recent`, `quality_fallbacks_recent`, `reply_quality_normalizations_recent`, `reply_quality_fallbacks_recent`
- `reply_fallback_focus_missing_recent`, `reply_fallback_direct_reaction_recent`, `reply_retry_kept_recent`
- `reply_flat_generic_tail_recent`, `reply_flat_voice_recent`, `reply_flat_reused_tail_recent`
- `reply_variety_press_recent`, `reply_variety_condition_recent`, `reply_variety_redirect_recent`
- `reply_reused_opening_recent`, `reply_reused_ending_recent`

`reply_fallback_direct_reaction_recent` は current runtime では target 名再掲不足だけでなく、
focus family に沿う first-sentence reaction cue が立たないケースも含む。
- `signal_visibility_misses_recent`, `objective_visibility_misses_recent`
- `signal_visibility_retry_recent`, `objective_visibility_retry_recent`

`reply_fallback_focus_missing_recent` は current runtime では required token 不足だけでなく、
focus family の cue / anchor が 1 文目に立たないケースと、family と無関係な drift を含む。
- `closed_scenes_without_arc_recent`, `scene_arcs_without_novel_recent`, `scene_close_backlog_recent`
- `scene_close_missing_arc_recent`, `scene_close_missing_novel_recent`, `scene_close_backlog_recovered_recent`

`tools.runtime_probe_story_events --goal scene_close` の current success 条件は、
「new closed scene が出た」だけではなく `story_arc` と `novel_output` まで揃うことにある。
返却 JSON では `scene_close_goal_stage` が次のいずれかを返す。

- `no_close`
- `closed_only`
- `arc_created`
- `complete`

あわせて current probe は `closed_only_count`, `arc_created_count`, `complete_count`, `backlog_recovered_scene_ids` を返し、
newly closed scene と backlog recovery の進み方を区別して読める。
reply quality path の current note:

- `reply focus contract` は返答成立性を扱う
- `reply signal contract` は dominant signal / scene objective の可視化を扱う
- `reply surface contract` は generic tail / voice flattening / recent self tail reuse を扱う
- `reply variety contract` は opening / ending の再利用と `press` / `condition` / `redirect` の shape 偏りを扱う
- `voice_flat_reply` は soft residual と hard retry を持ち、`voice_flat_blocking=true` のときだけ keep-path から外す
- `generation_quality_issue.details` には `reply_visibility_mode`, `signal_anchor_tokens`, `objective_anchor_tokens`, `signal_visible`, `objective_visible`, `reply_surface_mode`, `flat_issue_family`, `recent_self_tail_reused`, `reply_variety_shape`, `reply_variety_second_beat`, `recent_opening_reused`, `recent_ending_reused` が残る
- `scene_arcs_recent`, `novel_outputs_recent`
- warning 群 (`turn_stalled`, `reply_quality_fallback_high`, `reply_quality_flat`, `scene_close_weak`, `relationship_mode_flat`, `chapter_progress_stalled`, `director_satisfaction_low` など)

`tools.generate_story_review_draft` は monitor snapshot と DB snapshot を合わせて `Gaps`, `Cause Lens`, `Recommended Experiments`, `Next Checkpoints` を出力する。
current の review では chapter stagnation と director satisfaction axis も gap/cause に含め、`quality_gate_suppression` と `chapter_pressure_not_converting` を切り分ける。
reply fallback 系では `fallback_root_issue` と `retry_kept_without_fallback` を使い、
focus miss 起点の fallback か、direct reaction 不足起点の fallback か、soft residual の keep-path で吸えているかを観測できる。
signal visibility 系では `signal_visibility_missing` / `scene_objective_visibility_missing` と
その warning count を使い、reply は成立しているのに押しどころが見えないケースを観測できる。

post-round artifact の deterministic な truth-finding には `tools.runtime_probe_story_events` を使う。
これは `/tmp` DB copy 上で round 単位に engine を進め、`scene_close`, `growth`, `pattern`, `relationship mode`, `canon`, `pressure`, `episode` の増分を JSON で返す。

## 失敗時の扱い

### Story task

`ProcessManager` は task 完了時に例外を error ログへ出す。  
story task がクラッシュしても、自動再起動機構は current implementation にはない。

### WebPoster

HTTP 送信に失敗すると retry し、連続失敗数が閾値に達すると一定時間 pause する。  
pause 中も process 自体は生きており、時間経過で復帰する。

### LLM

LLM 呼び出し例外は provider client から上がる。  
`StoryEngine` 側の特別扱いは empty-final recovery であり、recovery 後も本文が空ならそのターンは chat log を保存せず消費する。  
ただし place 判定だけは process-local runtime snapshot に残り、同じ engine process 内の会話 session / narration 判定で使われる。

### Story-emergence state

scene/hook/growth/director 系は code 上は実装済みでも、flag が無効なら state をほぼ生成しない。  
そのため real DB review で `story_hooks`, `narrative_tensions`, `story_arc` などが 0 件でも、即「コード未実装」とは限らず、runtime config が旧経路のままの可能性がある。

また current director は、LLM が新規 tension を返さなくても
open hook / closed scene / relationship event から deterministic に `narrative_tensions` を seed する。
そのため current の live review では「hooks はあるのに tensions が 0」の場合、
まず config 無効化ではなく director 呼び出し条件と seed source の有無を疑う。

## current implementation の重要な前提

- public viewer は `WebPoster` 成功後の `.dat` しか見ない
- `config/web_post_targets.local.yaml` に story ごとの投稿設定が無い場合、`enabled=true` の story は `engine.main` では起動前に失敗し、hosted admin ではその story の start/restart が失敗する
- published archive viewer は DB ではなく `web/published` の JSON を読む
- `DatabaseManager` は single connection 共有であり、別 thread / 別 process 共有前提ではない
- `DatabaseManager.backend_kind` は `aiosqlite` または `sync_sqlite` で、admin `healthz` から観測できる
- multi-story 実行時も local Ollama は直列で処理される
- story-emergence 層は current implementation では opt-in であり、`config.yaml.example` の既定値も `enabled=false` が多い
- graceful stop はあるが、crash restart や durable job queue はない
- 失敗ターンの place snapshot は durable ではなく、process restart をまたいで保持されない

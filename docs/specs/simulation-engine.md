# PocketRole シミュレーションエンジン仕様

更新日: 2026-04-10

## 目的

`StoryEngine` の current behavior をステップ単位で明文化し、会話・移動・感情・LLM 連携の実装境界を固定する。

## この文書の正本範囲

- `engine/story_engine.py`
- `engine/scheduler.py`
- `engine/emotion_manager.py`
- `engine/place_manager.py`
- `engine/secret_manager.py`
- `engine/llm/base.py`, `engine/llm/router.py`, `engine/llm/ollama_client.py`

## 更新基準

ターン順序、会話 session ルール、LLM recovery、感情計算が変わったら更新する。

## 関連資料

- [runtime-architecture.md](./runtime-architecture.md)
- [data-model.md](./data-model.md)
- [../BUG_FIX.md](../BUG_FIX.md)

## 実行モード

current `StoryEngine` には 2 系統の実行モードがある。

### legacy path

- `scene_management` / `participation_planner` が無効なときの既定経路
- round ごとに全キャラを 1 周 `run_one_turn()` する
- 従来の `conversation_sessions` と `Scheduler` が主な会話制御になる

### scene-aware path

- `scene_management.enabled=true` かつ `participation_planner.enabled=true` のときの経路
- round 開始時に `story_scenes` と `scene_participants` を同期し、place ごとに active scene を維持する
- `ParticipationPlanner` が focus / support / observer を選び、planned speaker queue だけ `run_one_turn()` する
- solo scene はまれな条件でだけ作る

以下では共通処理に加え、flag 有効時の差分も current behavior として扱う。

## 1 ターンの固定ステップ

current `run_one_turn()` は次の 17 ステップで進む。

1. legacy path ではラウンドロビン、scene-aware path では planner queue から発言キャラを選ぶ
2. `character_states` から最新 state を読む。なければ character 定義から初期 state を作る
3. 感情を中立値 0.5 方向へ減衰させる
4. 時刻と weekday/weekend から推奨場所を計算する
5. 移動するか判定する
6. 実際に移動するなら `current_place` と `previous_place` を更新し、`moved` trigger を適用する
7. 同一場所の他キャラを集める
8. 異常ルールと照合する
9. `anomaly_detected`, `alone_long`, `talked_to` を条件に応じて適用する
10. 秘密開示ヒントと ambient context を計算する
11. 発言種別と会話 session 文脈を解決する
12. 現在感情から表情ラベルを決める
13. feature flag 有効時は active hook, relationship summary, intervention, effective profile, relationship mode, canon bit, dramatic pressure, dominant signal, scene objective を prompt context に注入する
14. prompt を作って LLM を呼ぶ
15. `quality_guard` 有効時は本文を検査し、必要なら normalize / retry / fallback / drop を行う
16. 成功本文を `chat_logs` と `character_states` へ保存する
17. 保存直後に hook 抽出、relationship update、scene participant stats 更新を行う

補足:

- `character_states` は保存済みの committed state であり、LLM 失敗ターンの未保存移動までは持たない
- current implementation は process-local な runtime place snapshot を持ち、同一 process 内では失敗ターン後の場所近似に使う

## ラウンド進行と時刻進行

- 1 回の `run_one_turn()` では 1 キャラだけが進む
- legacy path では `_char_index` がキャラ数ぶん進んだら 1 ラウンド完了になる
- scene-aware path では planner queue を消化しきると 1 ラウンド完了になる
- そのタイミングで `stories.last_sim_time` も更新される

つまり、`chat_logs.turn_number` は world clock のターン番号であり、キャラごとの個別カウンタではない。

## 感情システム

現在扱う感情キーは固定で 4 つ。

- `stress`
- `motivation`
- `loneliness`
- `excitement`

感情値は 0.0 から 1.0 に clamp される。  
自然減衰は `new = current + (0.5 - current) * 0.05` で、`engine.memory_limit` 等の config 値はこの current implementation では直接使われていない。

### 現在使う trigger

- `moved`
- `talked_to`
- `alone_long`
- `anomaly_detected`
- `event_day`
- `memory_recall`
- `secret_unlocked`

ただし current `StoryEngine` が実際に呼んでいるのは主に `moved`, `talked_to`, `alone_long`, `anomaly_detected` である。

### 表情判定

表情は感情値から rule-based に決まる。  
代表値は `happy`, `angry`, `sad`, `surprised`, `worried`, `content`, `lonely`, `tired`, `neutral`。

## 移動と場所判定

### 推奨場所

`PlaceManager.get_recommended_place()` は、現在の `sim_datetime` と `time_schedules.day_type` からマッチした最初の `expected_places[0]` を返す。  
複数候補があっても first choice だけが使われる。

### 実際の移動

`MoveEngine` は current 実装で弱い `social gravity` を持つ。

- 同席中は離脱しにくい
- 単独で、隣接先に active conversation / 多人数 / counterpart / active episode focus があると寄りやすい
- `social_target_place` が隣接していれば destination 選好で最優先する

隣接先がなければ現在地のままになる。移動した場合だけ `move_reason` を保存し、`previous_place` を更新する。

### solo scene の current 制約

solo scene は「1 人なら即開く」仕様ではない。

- 直前ターンが `monologue` なら連続開幕しない
- `stress >= 0.6` または `loneliness >= 0.6` または `motivation >= 0.85` が必要
- 原則として `2 consecutive alone turns` が必要
- unresolved hook / active tension がある場合だけ、例外的に早めに開くことがある

### 同一場所キャラの算出

current implementation は DB 全検索ではなく、process-local な runtime place snapshot と各キャラの最新 state から近似的に現在地 snapshot を組み立てる。
コード内にもある通り、これは current simplification であり、厳密な同時刻 occupancy 判定ではない。

## 異常検知

異常ルールは `anomaly_rules.condition_json` を見て最初にマッチした 1 件だけ返す。  
現在の判定に使われる条件は次の通り。

- `place`
- `zone`
- `time_from` / `time_to`
- `alone`
- `multiple_characters`
- `stress_threshold_min`

`consecutive_days_min` や `expected_place` のような継続条件は current implementation では判定していない。

## 会話タイプと conversation session

### Scheduler の初期判定

`Scheduler.schedule_utterance()` は current 実装では `会話相手がいるなら reply 優先` に寄っている。

- 同じ場所に 0 人なら `monologue`
- 同じ場所に 1 人なら原則 `reply`
- 同じ場所に 2 人以上でも、opener 後や `speaker_intent=react` では `reply`
- `group` は「場を開く最初の 1 手」に限定され、連発しない

### session 解決時の上書き

`StoryEngine._resolve_conversation_turn()` で実際の会話文脈が上書きされる。

- 参加者が 2 人未満なら session は作らない
- 参加者が 2 人以上なら place ごとに active `conversation_sessions` を使う
- 参加者が 3 人以上でも、recent non-self log があれば `reply_to_log_id` 付き `reply` を優先する
- 会話文脈で `monologue` が来ても runtime 側で `reply` へ coercion する
- 参加者が 2 人なら trust 閾値ではなく会話文脈を優先して `reply` へ寄せる
- 新規 session 作成時は同じ place / current sim time 以前の seed logs を拾って session に attach する

つまり、Scheduler は最初の候補であって最終決定者ではない。

scene-aware path では、`conversation_sessions` だけが会話の正本ではない。  
物語上の場面管理は `story_scenes` / `scene_participants` が担い、`conversation_sessions` は文脈補助に寄る。

## 秘密開示

秘密ヒントは `secret_reveal_condition` の自由文から正規表現で信頼度閾値を抜き出して判定する。  
現在は `信頼度0.8` のような文字列があることを前提にしており、抽出できない条件文は無効扱いになる。

戻り値は `hint_level` と `hint_text` で、prompt 注入に使う。  
DB 上で秘密が別テーブル管理されているわけではない。

## ambient context

`AmbientContextManager` は短期環境コンテキストを解決し、prompt に渡す。
現在は `ambient_states` を durable store とし、turn ごとに body state と place/ambient 要因をまとめる。

prompt builder へ渡る主な要素:

- `body_state_texts`
- `ambient_texts`
- `emotion_delta`

## prompt 構築

prompt builder へ渡す current context は次を含む。

- キャラ基本設定
- 現在時刻
- 場所名
- 感情値
- 表情
- 移動理由
- 異常情報
- body state / ambient context
- 同じ場所の相手名
- target 名
- recent dialogue lines
- secret hint
- active hooks
- relationship summary
- active interventions
- character profile overlay
- relationship mode texts
- canon bit texts
- dramatic pressure texts
- dominant signal text
- scene objective text
- speaker intent

装飾抑制の current rule として、character speech prompt には Markdown 記法、`#`, `*`, `**`, 箇条書きなどの装飾を使わないよう明示ルールが入っている。

`msg_type` に応じて `build_monologue_prompt`, `build_reply_prompt`, `build_group_prompt` を使い分ける。
effective profile は current code では `growth overlay > canon overlay > base character` の順で merge される。

reply path では current implementation が `reply_focus_text` から軽量な `reply focus contract` を組み立て、
`QualityGuard` と retry prompt の両方で共有する。
この contract は `focus_family`, `required_tokens`, `preferred_action`, `must_answer_in_first_sentence` を持ち、
focus miss は hard issue、direct reaction 不足だけのケースは soft residual として扱う。

## LLM 呼び出しと recovery

### Response 契約

`LLMResponse` は current implementation で次を持つ。

- `text`
- `model`, `provider`
- token count
- `latency_ms`
- `reasoning_present`, `reasoning_chars`
- `done_reason`
- `completion_status`

`LLMResponse.final_text` 自体は raw の `text` を返す。  
ただし `StoryEngine` は保存・quality・recovery 判定に使う前に、
`<think>...</think>` や先頭の reasoning residue を deterministic に sanitize する。

reply retry では、retry 後本文に `reply_direct_reaction_soft`, `voice_flat_reply`, `generic_reply_tail` などの
surface issue だけが残る場合、current implementation は deterministic fallback に落とさず通常保存へ戻す。
逆に `reply_focus_missing`, `reply_without_direct_reaction`, `scene_objective_drift`, `signal_override` が残る場合は keep しない。
`reply_focus_missing` の current behavior は token hit だけでなく、`reply_focus_contract` の
`required_focus_cues` / `focus_anchor_tokens` / `forbidden_focus_drift_tokens` も見る。
そのため focus family に沿う cue が 1 文目に見えていれば通常保存へ残し、
逆に token が偶然入っていても family と無関係な drift だけなら retry 側へ戻す。
`reply_without_direct_reaction` の current behavior も target 名の再掲や dialogue token 共有だけに依存しない。
`reply_focus_contract` の family-aware cue が 1 文目に立っていれば direct reaction 成立として扱い、
逆に focus 自体は見えていても相手への返しが generic な説明に流れる場合は hard retry に残す。

加えて current implementation では、reply path に `reply surface contract` があり、
`surface_mode`, `preferred_voice_cues`, `must_vary_from_recent_self`, `recent_self_endings` を使って
「成立はしているが毎回同じ締めになる」flat reply を別経路で観測する。
`voice_flat_reply` は常に hard retry ではなく、surface / variety / dramatic / shape / quality contract の
blocking miss を伴うケースだけ retry に上がる。
逆に story flavor weak や soft landing 単独のような residual は keep-path に残し、
monitor / review で追う。

さらに current implementation では `reply variety contract` も併用する。
この contract は `response_shape`, `second_beat_mode`, `forbidden_recent_endings`,
`forbidden_recent_openings`, `recent_second_beat_history`, `forbidden_recent_second_beats`,
`preferred_move_tokens` を持ち、retry prompt / deterministic fallback / flatness 判定の共通根拠になる。
目的は tone を派手にすることではなく、保存済み reply が毎回同じ opening / ending / 2文目の運びへ収束するのを抑えることにある。

加えて current implementation では `reply signal contract` も併用する。
この contract は `signal_anchor_tokens`, `objective_anchor_tokens`, `visibility_mode`,
`must_surface_in_first_two_sentences`, `forbidden_generic_replacements` を持ち、
dominant signal と scene objective が saved reply の 1-2 文内に見えるかを deterministic に判定する。
目的は台詞を派手にすることではなく、「その場の押しどころ」と「何を決める場か」を saved reply から読めるようにすることにある。

加えて current implementation では `reply dramatic contract` も併用する。
この contract は `move_mode`, `allowed_secondary_modes`, `required_move_tokens`,
`must_change_pressure_in_second_sentence`, `forbidden_soft_landings` を持ち、
signal visibility が通った reply でも bland な soft landing だけで閉じないようにする。
issue code は増やさず、`voice_flat_reply` の details に dramatic miss を残して runtime / monitor / review で追う。

さらに current implementation では `saved-reply shaping` 用の `reply blandness contract` も併用する。
この contract は `primary_shape`, `secondary_shape`, `must_shift_pressure_in_second_sentence`,
`forbidden_soft_landings`, `recent_shape_history` を持ち、
keep-path に残る reply が `same shape + pressure shift なし` の bland な着地に寄る場合だけ retry 側へ戻す。
目的は fallback を増やすことではなく、saved reply の blandness を runtime 側で圧縮することにある。

さらに current implementation では `reply shape contract` も併用する。
この contract は `primary_shape`, `secondary_shape`, `must_shift_pressure_in_second_sentence`,
`forbidden_soft_landings`, `recent_shape_history`, `story_pressure_tokens` を持ち、
saved reply が same shape の再利用に寄るだけでなく、scene / pressure の押しどころを 2文目で見せられているかも追う。
story flavor は freeform template ではなく、dominant signal / scene objective / pressure type からの deterministic token cue で与える。

さらに current implementation では `reply quality contract` も併用する。
この contract は `primary_shape`, `second_beat_mode`, `story_pressure_tokens`,
`forbidden_soft_landings`, `recent_shape_history`, `recent_second_beat_history`,
`required_story_flavor_role` を持ち、signal visibility が成立した後でも
`same shape + repeated second beat` や `story flavor weak` が残る reply を hard/soft に振り分ける。
目的は fallback を増やすことではなく、keep-path に残る bland reply を long-run で圧縮することにある。

さらに current implementation では `reply story quality contract` も併用する。
この contract は `reply quality contract` を story pressure cue と束ねた最終段で、
`story_pressure_cue_text` を持ち、retry prompt / deterministic fallback / keep-path で共有される。
目的は signal visibility が通った後でも、2文目が story 固有の争点を持たず soft landing に流れる窓を減らすことにある。

`reply dramatic contract` の current behavior は token hit だけではなく、
`semantic_move_family` と `pressure_anchor_tokens` も併用して
「2文目がその場の争点を差し戻す / 条件化する / 問い返す / 言い切る」のどれをしているかを見る。
token は合っていても anchor が無い reply は成立扱いにせず、
`voice_flat_reply.details.reply_dramatic_semantic_*` に診断を残して retry 側へ戻す。

さらに current implementation では `reply residual quality contract` も併用する。
この contract は `reply story quality contract` を keep-path 最終判定向けに写した薄い段で、
`residual_block_threshold=shape+second_beat+pressure_shift_or_story_flavor` を持つ。
`same shape + repeated second beat + no pressure shift` または `soft landing + no story flavor` が残る reply は、
issue code を増やさず `voice_flat_reply.details.reply_residual_*` に診断情報を残して retry 側へ戻す。

scene-close artifact handoff では current implementation が newly closed scene の fast path に加え、
recent closed conversation scene の backlog も見る。
`missing_arc` は scene / recent logs / hooks / tensions から context を再構築して arc から作り、
`missing_novel_output` は existing arc を再利用して prose だけ補完する。
このとき recent logs が薄くても、existing arc の title / summary を scene-close context と
deterministic fallback の seed に戻し、duplicate arc を増やさず `novel_output` completion を優先する。

### Qwen3 系の built-in profile

`LLMRouter` は `provider == ollama` かつ `model.startswith("qwen3")` に対して built-in profile を持つ。

- `reasoning_mode = auto`
- `max_tokens = 1000`
- `recovery_max_tokens = 1400`
- `recovery_reasoning_mode = off`

### empty-final recovery

primary 応答の sanitize 後本文が空なら、`StoryEngine` は 1 回だけ recovery variant で再実行する。  
recovery 後も sanitize 後本文が空なら、そのターンは `chat_logs` を保存せずに消費する。

注意点は次の通り。

- raw thinking は DB に保存しない
- `<think>` や先頭 leakage は保存前に sanitize する
- empty-final は invalid completion と扱う
- recovery がない profile ではそのまま skip する
- そのターンの place は process-local snapshot にだけ反映され、`character_states` には保存されない

## quality normalization と fallback

current quality flow は soft issue と hard issue を分ける。

- soft issue
  - `paragraph_break_output`
  - `markdown_formatting`
  - 軽い `sentence_overflow`
  - 軽い `overlength`
- hard issue
  - `poetic_abstraction`
  - `abstract_opening`
  - `low_concreteness`
  - `scene_objective_drift`
  - `signal_override`
  - `reply_without_direct_reaction`
  - `multi_clause_heaviness`
  - `monologue_in_conversation_context`

soft issue だけなら本文を正規化して通常生成のまま保存する。hard issue は retry し、2回失敗時だけ deterministic fallback を使う。
fallback は current code では voice-aware で、`first_person`, `speech_style`, `speech_examples`, `dominant_signal_text`, `scene_objective_text`, recent self logs を軽く参照する。
reply fallback では recent self の第2文と同じ締めを避け、可能なら family 別の別パターンへ切り替える。
retry / fallback の current goal は「とにかく派手に言い換える」ではなく、
focus を満たした上で `press` / `condition` / `redirect` のどれかを 2 文目に持たせ、
recent self の opening / ending / second beat の再利用を避けることに置かれている。
さらに signal visibility slice では、retry / fallback が `張り合い`, `順番`, `本音` のような anchor を
1-2 文目へ戻すように寄せられている。

## 永続化されるもの

本文生成に成功したターンでは最低限次が保存される。

- `chat_logs`
  - `message`, `msg_type`, `place_id`, `expression`, `llm_provider`, `llm_model`, `conversation_session_id`, `reply_to_log_id`, `scene_id`, `speaker_intent`, `quality_score`, `quality_flags`, `posted_to_web=0`
- `character_states`
  - current/previous place, current expression, move reason, emotion values

会話 session がある場合は `conversation_sessions` の last speaker / last activity も更新される。
ambient 要因が新規発生した場合は `ambient_states` に保存される。

flag 有効時はさらに次が更新対象になる。

- `story_scenes`, `scene_participants`
- `story_hooks`
- `story_interaction_patterns`
- `story_episodes`
- `relationships`, `relationship_events`
- `generation_quality_issues`
- `character_growth_candidates`, `character_profile_overlays`, `character_evolution`
- `narrative_tensions`, `director_interventions`
- `story_arc`, `novel_output`

closed conversation scene の `scene_close` prose handoff は current implementation で same-round の queue だけに依存しない。
`StoryEngine` は直近 closed conversation scene を再走査し、`story_arc` が無い scene と
`story_arc` はあるが `novel_output` が無い scene を backlog として再回収する。
そのため scene close の後段は次の 3 段で見なす。

- `missing_arc`
- `missing_novel_output`
- `complete`

`NovelGenerator` の `scene_close` 経路も partial handoff を許容し、
既存 `source_scene_id` の arc がある場合は duplicate arc を作らず prose だけ補完する。
`scene_outcome_summary` や `recent_log_lines` が弱い場合は、scene metadata / participant / hook / tension から fallback summary を組み立てる。

`StoryDirector` の current runtime では、`narrative_tensions` は LLM だけで起票されるわけではない。
analysis round ではまず deterministic pass が走り、少なくとも次を seed source に使う。

- open `story_hooks`
- recent closed conversation scene の `outcome_summary`
- recent `relationship_events`

その後で LLM analysis が既存 tension の update / merge / intervention 生成を行う。
analysis が intervention を返さない場合は、active tension を材料に fallback intervention を作る経路がある。

current runtime ではその前段として、deterministic な `interaction pattern` 層が入っている。

- source は open hook / active tension / recent closed conversation scene / recent relationship event
- active pattern は `StoryDirector` の tension / intervention ranking に使われる
- `RelationshipModeEngine` は directed pair ごとに cluster 単位で最大 2 active modes を維持し、decay を段階的に適用する
- `EmergentCanonizer` は canon ladder を維持し、`CanonReignitionEngine` は `recurring_bit` 以上から hook を再発火する
- `DramaticPressureEvaluator` は pattern / relationship mode / canon / episode を上位解釈して active pressure を維持する
- さらに `EpisodePlanner` が pattern / hook / tension cluster から 1 story 1 active episode を維持する
- active episode は round plan, social target, director context, `episode_close` prose に弱く効く

`ConversationMotifEngine` はこのさらに手前で、組み込み会話発生パターンを弱く適用する。
current implementation では `solo_seed_rondo` のみを扱い、open `solo_seed` hook から
`conversation_motif_runs` を作る。進行中 run は、同じ場所にいる別キャラへ独り言を拾わせ、
次に元キャラへ戻し、その後の会話連鎖へ渡すために `msg_type` / reply target / hook text を補助する。
台詞内容そのものは固定せず、メタ語彙も prompt へ出さない。

## current simplifications と注意点

- same-place 判定は strict な時刻整合ではなく近似である
- story-emergence 層は code 上は存在するが、runtime config で明示有効化しないと legacy path のまま動く
- `event_calendar` は `WorldClock` 構築時に未接続で、event-driven branch は薄い
- `WebPoster` は `StoryEngine` に DI されているが、turn 内で直接送信はしない
- model が `<think>` を本文へ混ぜた場合、それを除去する post-process は current implementation にはない
- `director_interventions` は current 実装上は deterministic fallback 付きだが、real run では short observation だけで必ず立つとは限らない
- growth は malformed JSON で skip しうるが、current code では no-write / rollback で安全化されている

# PocketRole Story Quality Playbook

更新日: 2026-04-10

## 目的

この文書は、PocketRole の story / character 設定、runtime config、実運転レビューを見直すときに、
「どうすればチャットボット感を減らし、物語性と出力品質を高められるか」を素早く参照するためのハンドブックである。

実装詳細の正本は `docs/specs/` 各文書にある。  
このファイルは「どこを疑うか」「どう直すか」を整理する運用寄りのガイドとして使う。

## まず確認する runtime 前提

story-emergence 系の code が入っていても、`config.yaml` の feature flag が無効なままだと旧来の挙動に寄りやすい。

まず次を確認する。

- `scene_management`
- `participation_planner`
- `story_hooks`
- `relationship_dynamics`
- `quality_guard`
- `growth_engine`
- `episode_planner`

加えて、長時間運転では次を前提に観測する。

- `python -m tools.monitor_engine_runtime`
- `python -m tools.generate_story_review_draft`
- `python -m tools.runtime_probe_story_events`

ここが無いままログだけ読むと、「モデルが悪い」のか「runtime が旧経路のまま」なのかを切り分けにくい。

2026-04-03 時点では `generate_story_review_draft` に `Cause Lens` が入り、gap の列挙だけでなく
「なぜ flat なのか」と「次に試す 1 手」を review-first で読める。

同日 final eval の current truth では、`ankoku_gakuen` は次の状態にある。

- `reply_ratio=0.70`
- `sessionless_monologue_ratio=0.07`
- `growth_commits_recent=39`
- `quality_fallbacks_recent=37`
- `reply_quality_fallbacks_recent=29`
- `reply_focus_misses_recent=24`
- `growth_parse_failures_recent=14`

したがって current の優先順位は、
「会話を増やす」より「visible quality の残差をどこまで実装で詰めるかを見極める」ことに移っている。

## ストーリー性を高めるコツ

### 前提: 設定調整で改善できることと、engine 側 upgrade が必要なことを分ける

story YAML、character seed、prompt、runtime config の調整だけでも、
局所的な会話品質や tone はかなり改善できる。
ただし、それだけで「作品意図に沿って面白さが自己増幅し続ける system」になるわけではない。

切り分けの目安:

- 設定調整で効くもの
  - キャラの役割分担
  - scene の争点
  - 発話の tone
  - hook の立ちやすさ
- engine upgrade が必要なもの
  - 作品意図そのものの保持
  - 面白い偶然の canon 化
  - 関係モードの継続
  - episode 単位の進展制御

後者は `docs/specs/known-gaps.md` の「物語増幅レイヤーの不足」を参照する。

### 1. キャラを似た抽象テーマで並べない

物語が弱いときは、キャラ全員が似た感傷語彙で同じ話題を回していることが多い。

初期 seed では、各キャラに少なくとも次を非対称に持たせる。

- 今ほしいもの
- 今避けたいこと
- 触れられたくない話題
- 誰に近づきたいか
- 誰を警戒しているか

「全員が繊細」「全員が孤独」「全員が詩的」だと、文体差は出ても物語上の衝突が弱い。

### 2. 会話より先に scene の争点を作る

会話が雰囲気だけで終わるときは、scene に次が不足している。

- 今回その場で決まること
- その場で避けたいこと
- 誰が主導権を握りたいか
- scene の終わりに残る未解決課題

会話は scene の結果として起こるべきであり、scene 自体に目的がないと round-robin chat に戻りやすい。

### 3. hook を立てやすい発話を増やす

物語が継続しないときは、発話に次の要素が少ないことが多い。

- 質問
- 約束
- 対立
- 保留
- 次の行動提案

`story_hooks` を増やしたいなら、キャラ設定にも prompt にも「言い切らずに持ち越す火種」を含める必要がある。

### 4. 関係の歪みを初期から持たせる

`relationships` が全組み合わせで似た状態から始まると、会話が平板になる。

最低限、story 開始時から次のどれかを持たせると scene が立ちやすい。

- 一方的な憧れ
- 過去の借り
- 軽い不信
- 秘密の共有
- 価値観の食い違い

### 5. 成長は「よい変化」だけにしない

`growth_engine` を活かすなら、変化の候補が
「勇気を得る」「前向きになる」だけだと単調になる。

次のような変化も許容する。

- 疑い深くなる
- ある人物だけには心を開く
- 目的の優先順位が変わる
- 守りたいものが増える
- 逆に一時的に閉じる

## アウトプットの質を高めるコツ

### 1. まず `reply_ratio`, `sessionless_monologue_ratio`, `all_chars_spoke_ratio` を見る

ログがチャットボットっぽいとき、最初に見るべき指標はこの2つである。

- `reply_ratio`
  - 低すぎると、会話の往復より独演が多い
- `sessionless_monologue_ratio`
  - 高すぎると、会話 session の外で 1 人発話が溜まりすぎている
- `all_chars_spoke_ratio`
  - 高すぎると、毎ターン全員参加の round-robin に戻っている
- `quality_normalizations_recent` / `quality_fallbacks_recent`
  - 高すぎると、通常生成が quality gate に吸われすぎている
- `reply_quality_normalizations_recent` / `reply_quality_fallbacks_recent`
  - reply だけが gate で失速していないかを見る
- `reply_fallback_focus_missing_recent` / `reply_fallback_direct_reaction_recent`
  - reply fallback の主因が focus miss なのか、相手への直接反応不足なのかを切り分ける
- `reply_retry_kept_recent`
  - soft residual だけの retry 結果を fallback せず通常保存へ戻せているかを見る
- `reply_flat_generic_tail_recent` / `reply_flat_voice_recent` / `reply_flat_reused_tail_recent`
  - `reply_quality_flat` の中でも、汎用締めが多いのか、voice が平板なのか、直近 self tail を繰り返しているのかを切り分ける
- `reply_variety_press_recent` / `reply_variety_condition_recent` / `reply_variety_redirect_recent`
  - keep-path で残せた reply が 1 つの運び方に偏っていないかを見る
- `reply_dramatic_move_missing_recent` / `reply_soft_landing_recent` / `reply_shape_dominance_recent`
  - signal が見えていても bland に着地していないか、2文目の pressure change が弱くないかを見る
- `reply_bland_shape_reused_recent` / `reply_pressure_shift_missing_recent` / `reply_story_flavor_weak_recent` / `reply_second_beat_reused_recent` / `reply_quality_keep_blocked_recent` / `reply_bland_keep_blocked_recent`
  - keep-path に残る reply が same shape や same second beat の繰り返しへ寄っていないか、runtime がどこまで bland reply を block できているかを見る
- `reply_story_quality_keep_blocked_recent`
  - story pressure cue まで見た最終 contract で keep-path から弾かれた bland reply の窓を確認する
- `reply_residual_keep_blocked_recent`
  - signal / story pressure が一度見えていても、最後の residual contract で `same shape + repeated second beat + no pressure shift` または `soft landing + no story flavor` と判定された reply を追う
- `reply_reused_opening_recent` / `reply_reused_ending_recent` / `reply_reused_second_beat_recent`
  - long-run で同じ切り出しや同じ着地、同じ 2 文目の運びへ収束していないかを見る
- `signal_visibility_misses_recent` / `objective_visibility_misses_recent`
  - reply が保存されていても dominant signal や scene objective が表に出ていないかを見る
- `signal_visibility_retry_recent` / `objective_visibility_retry_recent`
  - visibility miss が normalize ではなく hard retry へ上がる窓を確認する

目安として、`reply_ratio` が低く、`sessionless_monologue_ratio` や `all_chars_spoke_ratio` が高いときは、
モデルより先に planner と scene 設計を疑う方がよい。

### 2. 発話を「短く、具体的に、1意図」に寄せる

出力品質が落ちるときは、次の症状が出やすい。

- 長すぎる
- 抽象語が多い
- 相手への反応ではなく自己演説になる
- 同じ比喩が再帰する

対策としては次を優先する。

- `quality_guard` の上限を守らせる
- `participation_planner` で発話人数を絞る
- prompt で「1発話1意図」を強める
- 具体観察、反応、行動提案のいずれかを必須に寄せる

### 2.5. gap だけでなく Cause Lens で読む

current review draft の `Cause Lens` は、個別 gap を上位原因へ束ねて 3 件までに圧縮する。

主な cause family:

- `signal_visibility_weak`
- `scene_purpose_not_visible`
- `quality_gate_suppression`
- `canon_carryover_weak`
- `growth_not_sticking`
- `turnflow_weak`

review では各 cause ごとに次を読む。

- `Why`
- `Evidence`
- `Next experiment`

優先順位は `Gaps` の件数ではなく `Cause Lens` の上位 1-2 件で決める。

補足:

- `reply_ratio` と `sessionless_monologue_ratio` が既に良いときは、`turnflow_weak` を追わず `signal_visibility_weak` と `quality_gate_suppression` を先に見る
- `reply_quality_fallback_high` が残る場合は、まず `reply_fallback_focus_missing_recent` と `reply_fallback_direct_reaction_recent` のどちらが大きいかを見る
- `reply_fallback_focus_missing_recent` が高いなら、required token だけでなく focus cue / anchor と family drift の判定を先に見る
- `reply_fallback_direct_reaction_recent` が高いなら、target 名の再掲不足より先に family-aware first sentence reaction が立っているかを見る
- `reply_retry_kept_recent` が増えているなら、fallback volume は runtime で圧縮できているので、次は `reply_quality_flat` 側を優先する
- `reply_quality_flat` を追うときは、まず `reply_reused_second_beat_recent` と `reply_flat_reused_tail_recent` を見て repeated second beat / ending 由来かを切り分け、その後 `reply_flat_generic_tail_recent` と `reply_flat_voice_recent` のどちらが支配的かを見る
- `reply_quality_flat` の residual が keep-path 側に寄っているときは、`reply_variety_press_recent` / `reply_variety_condition_recent` / `reply_variety_redirect_recent` の偏りを見る
- `reply_residual_keep_blocked_recent` が高いなら、signal visibility や story pressure cue は見えていても、最後の 2 文目が同じ運びと soft landing に戻っている
- `reply_dramatic_move_missing_recent` や `reply_soft_landing_recent` が高いなら、signal は見えていても 2文目の押し返しが弱い
- 2026-04-10 時点の current runtime では、`reply_dramatic_move_missing_recent` は token 不足だけでなく
  `semantic family は合うが pressure anchor が無い` reply も含む
- `reply_bland_shape_reused_recent` と `reply_pressure_shift_missing_recent` が高いなら、signal visibility ではなく saved reply の blandness が主因である
- `reply_second_beat_reused_recent` と `reply_quality_keep_blocked_recent` が高いなら、same shape に加えて repeated second beat が long-run 残差の主因である
- `reply_story_flavor_weak_recent` が高いなら、shape は見えていても story 固有の pressure が 2文目で弱く、scene の押しどころが generic に流れている
- `reply_story_quality_keep_blocked_recent` が高いなら、shape や second beat だけでなく story pressure cue を使った 2文目形成まで current runtime が block し始めている
- `reply_bland_keep_blocked_recent` が増えているなら、runtime が bland reply を keep-path から弾き始めている
- `reply_reused_opening_recent` と `reply_reused_ending_recent` が高いなら、voice の弱さよりも recent self repetition が主因である
- `signal_visibility_weak` が review 上位に来たら、`signal_visibility_misses_recent` と `objective_visibility_misses_recent` のどちらが大きいかを先に見る
- `signal_visibility_retry_recent` が高いなら、focus/surface/variety より前に dominant signal / scene objective の prompt 可視化を疑う
- `growth_parse_failures_recent` が review 窓で残っていても、新規 turn で増えていないなら parse-first fix 済みとして扱い、窓の古い failure と分けて読む
- `scene_close` probe は `scene_close_goal_stage` を先に見る
- `closed_only` なら close は起きているが `story_arc` が足りない
- `arc_created` なら arc はあるが `novel_output` が足りない
- monitor では `closed_scenes_without_arc_recent`, `scene_arcs_without_novel_recent`, `scene_close_backlog_recent` を見て、same-round failure なのか backlog 回収遅れなのかを分ける
- `closed_only` が続くなら、まず `scene_outcome_summary` と `participant_names` の handoff rebuild が薄くなっていないかを疑う
- `arc_created` が続くなら、existing arc summary/title reuse と prose fallback が薄くなっていないかを先に疑う

### 3. quality guard は「詩情の敵」ではなく「ノイズ除去」と考える

抽象語や装飾を抑えると味が消えるように見えるが、
実際には反復や自己模倣が減ることで、必要な場面だけ印象的な言い回しが残りやすい。

特に次の症状が出たら guard を疑う。

- 同じ抽象語の頻出
- 同じ n-gram の反復
- Markdown 風装飾の混入
- whitespace-only や極端な短文化

ただし 2026-04-03 final eval 時点では、visible quality の残差は guard 調整だけで取り切れていない。
`reply_focus_misses_recent` や `reply_quality_fallbacks_recent` が高止まりし続ける場合は、
それ以上の tuning を無限に続けず、「model / story / long prompt 競合の限界が混ざっている」と判断して
final evaluation か docs sync へ進むのが current practice である。

### 4. scene prose が出ないときは novel generator だけを疑わない

`story_arc` や `novel_output` が増えない場合、原因は prose generator そのものではなく、
前段の scene close や hook/tension の生成不足であることが多い。

次を順に見る。

1. `story_scenes` が閉じているか
2. `story_hooks` や `narrative_tensions` が残っているか
3. `story_arc.source_scene_id` が作られているか
4. そのうえで `novel_output` を確認する

## 症状別の診断表

### 毎ターン全員が話してしまう

- まず疑う設定:
  - `scene_management`
  - `participation_planner`
- 次に見る指標:
  - `all_chars_spoke_ratio`
  - `speaking_chars_per_turn_avg`
  - `active_scenes`
- 改善方向:
  - focus/support/observer の偏りを強める
  - scene に参加しないキャラを許容する

### 会話は増えるが物語が積み上がらない

- まず疑う設定:
  - `story_hooks`
  - `relationship_dynamics`
- 次に見る指標:
  - `open_hooks`
  - `hooks_resolved_recent`
  - `relationship_pairs_changed_recent`
- 改善方向:
  - 質問、約束、対立、保留の発話を増やす
  - 関係が変化する seed を強める

### growth 候補は出るがキャラが変わらない

- まず疑う設定:
  - `growth_engine`
- 次に見る指標:
  - `growth_candidates_pending`
  - `growth_commits_recent`
  - `growth_empty_recent`
  - `growth_quality_rejections_recent`
  - `growth_parse_failures_recent`
  - `growth_rollbacks_recent`
  - `character_profile_overlays`
- 改善方向:
  - empty / rejection / pending accumulation のどこで失速しているかを先に切り分ける
  - `current_goal`, `current_worry`, `personality_core` の field fit と durable evidence を見る

### canon はあるが戻ってこない、または profile に効かない

- まず疑う設定:
  - `episode_planner`
  - `growth_engine`
- 次に見る指標:
  - `active_canon_bits`
  - `canon_reignitions_recent`
  - `canon_triggered_hooks_open`
  - `canon_writebacks_recent`
  - `active_canon_profile_overlays`
- 改善方向:
  - `momentary_bit` ではなく `recurring_bit/proto_canon/canon` へ昇格できているかを見る
  - reignition cooldown や open hook の詰まりを確認する
  - canon overlay が growth overlay に潰されていないかを見る

### review で何から直すか迷う

- まず見る section:
  - `Cause Lens`
  - `Recommended Experiments`
- 次に見る指標:
  - `reply_focus_misses_recent`
  - `generic_reply_tails_recent`
  - `voice_flat_replies_recent`
  - `canon_reignitions_recent`
  - `canon_triggered_hooks_open`
  - `growth_empty_recent`
  - `growth_quality_rejections_recent`
- 改善方向:
  - `signal_visibility_weak` なら reply focus / target excerpt / voice anchor を優先
  - `scene_purpose_not_visible` なら scene objective と payoff push を優先
  - `quality_gate_suppression` なら normalize と hard retry 境界を優先
  - `canon_carryover_weak` なら reignition / writeback cadence を優先
  - `growth_not_sticking` なら empty / rejection / pending accumulation を優先

### prose が出ない、または薄い

- まず疑う設定:
  - scene close の発生
  - `story_director`
- 次に見る指標:
  - `closed_scenes_recent`
  - `scene_arcs_recent`
  - `novel_outputs_recent`
  - `active_tensions`
- 改善方向:
  - scene の終端で「何が変わったか」を残す
  - unresolved hook や tension を残したまま scene を閉じる

### 発話が抽象的で LLM 感が強い

- まず疑う設定:
  - `quality_guard`
- 次に見る指標:
  - `quality_issues_recent`
  - `quality_normalizations_recent`
  - `quality_fallbacks_recent`
  - `reply_quality_fallbacks_recent`
  - 直近ログの抽象語反復
- 改善方向:
  - 発話を短くする
  - 抽象語より具体観察と相手反応を増やす
  - seed の時点でキャラの関心対象を具体化する
  - `viewer` が止まって見えるときは web 側だけでなく quality gate suppression も先に疑う

## `ankoku_gakuen` からの短い実例

2026-03-24 の initial real DB review では、`ankoku_gakuen` を実運転して次が観測された。

- `reply_ratio=0.11`
- `all_chars_spoke_ratio=1.00`
- `story_hooks=0`
- `narrative_tensions=0`
- `story_arc=0`

このケースでは、モデルの文才より先に
「runtime flag が旧経路のままではないか」
「story seed が新 runtime を発火しやすい形になっているか」
を疑うべきだった。

その後、current runtime では次の変化が確認された。

- `all_chars_spoke_ratio` は `0.65` まで改善
- `story_hooks` は `12` 件まで起動
- `narrative_tensions` は deterministic seed で起動
- `director_interventions` は active / acknowledged を取りうる
- `scene_close` / `growth` / `pattern` / `episode` は probe で観測できる

つまり current の教訓は次である。

- hook が 0 なら、まず reply / handoff / explicit targeting を疑う
- hook があるのに tension が 0 なら、director seed source と analysis round 条件を疑う
- tension があるのに intervention が 0 なら、escalation / fallback 条件と観測 turn 数を疑う
- closed scene があるのに prose が無いなら、`smoke_engine_start` ではなく `runtime_probe_story_events` で post-round handoff を確認する
- pattern があるのに continuity が弱いなら、`active_episode_type`, `active_episode_age`, `episode_close_completion_rate` を先に見る

## 最後に

PocketRole の story quality は、モデルだけで決まらない。

- story / character seed
- runtime flag
- scene / hook / relationship / growth の発火条件
- monitor と review による観測

この4つを合わせて調整したときに、初めて「気の利いた発話」から「持続する物語」へ寄っていく。

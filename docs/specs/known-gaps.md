# PocketRole 既知課題と未充足仕様

更新日: 2026-04-09

## 目的

current implementation の制約と既知ギャップを一覧化し、次の改修計画で前提を誤らないようにする。

## この文書の正本範囲

- 現在の code から見える制約
- 直近の live 検証結果
- 既存 proposal / bug log に記録された未解消事項

## 更新基準

新しい制約が見つかったとき、あるいは既知課題が解消されたときに更新する。

## 関連資料

- [../BUG_FIX.md](../BUG_FIX.md)
- [../MODEL_OUTPUT_COMPARISON.md](../MODEL_OUTPUT_COMPARISON.md)
- [../WEB_API_VISIBILITY_PROPOSAL.md](../WEB_API_VISIBILITY_PROPOSAL.md)

## 1. LLM と出力品質

### current 推奨モデル

- 総合推奨: `ministral-3:14b`
- 安定性優先の代替: `qwen3:8b`
- 実験枠: `qwen3.5:9b`

### cloud model 運用上の current note

- OpenAI の `gpt-5-mini`, `gpt-5.4-mini`, `gpt-5.4-nano` は current code で smoke 成功済み
- ただし `/v1/models` の可視性だけでは生成 endpoint 互換を保証しないため、model 変更時は `smoke_openai_models` で 1 ターン以上確認する
- `gpt-5-mini` は `Responses API` で generic payload をそのまま投げると incomplete response に寄りやすいため、current implementation には model-specific な payload 補正が入っている

### 現在不採用の確認済みモデル

- `yuma-story:latest`
  - CPU / オフロード寄りで比較対象から除外
- `ministral-3:14b-story`
  - GPU 常駐が不安定で除外
- `yuma-safe:latest`
  - GPU 常駐自体は成功したが、本文へ `<think>` が混入し public 品質で不採用

### thinking 系モデルの制約

- Qwen3 系では empty-final recovery を前提にした吸収設計が必要
- model によっては thinking が `message.thinking` ではなく本文へ漏れる
- 保存前本文には `<think>...</think>` と先頭 reasoning residue の sanitize が入ったが、
  provider ごとの差分や漏れパターンの追加観測はまだ必要

## 2. シミュレーション精度

### story-emergence runtime の current gap

scene/hook/growth/director/novel 系は current code に入っているが、runtime config で明示有効化しないと旧経路のまま動く。

2026-03-24 の real DB review では、`ankoku_gakuen` を turn `46 -> 56` まで実運転しても次が観測された。

- `reply_ratio=0.11`
- `all_chars_spoke_ratio=1.00`
- `story_hooks=0`
- `narrative_tensions=0`
- `director_interventions=0`
- `story_arc=0`
- `novel_output=0`

その後 2026-04-03 時点の current runtime では、real DB / `/tmp` probe 上で次の改善が確認されている。

- `all_chars_spoke_ratio` は real DB final eval で `0.20` まで低下
- `reply_ratio` は real DB final eval で `0.70` まで改善
- `sessionless_monologue_ratio` は real DB final eval で `0.07` まで低下
- `story_hooks` は `0 -> 61` まで増加
- `relationship_events` は少数だが発生
- `narrative_tensions` は deterministic seed で起動する
- `director_interventions` は active / acknowledged を取りうち、real DB final eval 時点でも `live_interventions=1` を維持する
- `growth_engine` は real DB final eval で `growth_commits_recent=39`, `growth_rollbacks_recent=0` を維持する
- `growth` probe では `goal_reached=true`, `growth_commit_delta=1` を確認済み
- `pattern` と `episode` の persisted state が動作している
- `scene close` / `novel_output` は real DB で artifact 自体は残る
- `scene_close` probe は current config では `goal_reached=false`, `scene_arc_delta=0`, `novel_output_delta=0` のまま残る

一方で同時点でも次は未解消である。

- visible quality は依然として長時間 real run の支配課題で、final eval でも `quality_fallbacks_recent=37`, `reply_quality_fallbacks_recent=29`, `reply_focus_misses_recent=24`, `voice_flat_replies_recent=18` が残る
- 長時間 real run では会話の面白さが still flat に寄りやすく、残差は model / story / long prompt 競合の影響も強い
- `growth_engine` は parse-first safety により新規 failure を増やしにくくなったが、review 窓では `growth_parse_failures_recent=14` が残る
- `growth_engine` の long-run commit 安定性は改善したが、`growth_quality_rejections_recent` と pending/supersede の tuning 余地はまだある
- `scene_close` / `novel_output` は backlog recovery と partial handoff 補完が入り、probe/monitor でも `closed_only` / `arc_created` / `complete` と `backlog_recovered_scene_ids` を読めるようになったが、long-run で `scene_close_completion_rate` を安定して 0.50 以上に保つ課題は残る
- current code では `missing_arc` を先、`missing_novel_output` を後に回収し、scene-close context も recent logs ベースで再構築できるようになったが、real run で backlog recovery が steady に追いつくかは継続観測が必要
- review draft の `Cause Lens` は current tuning で `quality_gate_suppression`, `chapter_pressure_not_converting`, `director_alignment_low` まで拾えるようになったが、long-run 実運転との差分はまだ残る
- `relationship_mode` は active だが、final eval 時点でも `multi_mode_pairs_active=0` に戻る窓があり、複合 mode の長時間安定は未解消

したがって current の主問題は「新 runtime が全く動いていない」ことではなく、
「hook / tension / pattern / episode / relationship mode / canonizer / dramatic pressure と growth commit までは動いているが、
visible quality、scene-close long-run 達成、story-quality diagnosis の精度がまだ弱い」ことである。

2026-04-08 時点では observation surface 自体は増えており、monitor/review で次を直接読める。

- `reply_quality_fallback_high` と `reply_quality_flat` の切り分け
- `reply_fallback_focus_missing_recent` と `reply_fallback_direct_reaction_recent` による fallback root cause の切り分け
- `reply_retry_kept_recent` による soft residual keep-path の観測
- `reply_flat_generic_tail_recent`, `reply_flat_voice_recent`, `reply_flat_reused_tail_recent` による flat reply 内訳の切り分け
- `reply_variety_press_recent`, `reply_variety_condition_recent`, `reply_variety_redirect_recent` による saved reply shape 偏りの観測
- `reply_reused_opening_recent`, `reply_reused_ending_recent`, `reply_reused_second_beat_recent` による long-run repetition の観測
- `signal_visibility_misses_recent`, `objective_visibility_misses_recent` による dominant signal / scene objective 可視化不足の観測
- `signal_visibility_retry_recent`, `objective_visibility_retry_recent` による visibility miss の hard retry 窓
- `scene_close_weak` の weak case と `closed_scenes_without_arc_recent` / `scene_arcs_without_novel_recent` / `scene_close_backlog_recent`
- `scene_close_missing_arc_recent`, `scene_close_missing_novel_recent`, `scene_close_backlog_recovered_recent` による dominant cause の切り分け
- `relationship_mode_flat`
- `chapter_progress_stalled`
- `director_satisfaction_low` と pacing/surprise axis

ただし、これらは「診断面の改善」と fallback volume / flat reply / repeated ending の runtime 側圧縮であり、long-run 実運転の根本改善そのものではない。
current の visible quality 残差は、fallback 量そのものよりも「keep-path で残った reply の運び方が 1 パターンへ寄ること」と「scene の押しどころが saved reply に十分出ないこと」に重心が移っている。
current code では `reply blandness contract` と `reply shape contract`、`reply quality contract`、
`reply story quality contract`、`reply_bland_*` / `reply_story_flavor_weak_recent` /
`reply_second_beat_reused_recent` / `reply_quality_keep_blocked_recent` /
`reply_story_quality_keep_blocked_recent` metrics が入り、
same shape の繰り返しや pressure shift 不足に加え、story 固有の pressure が 2文目で弱い窓や repeated second beat も runtime / monitor / review で直接読めるようになった。
それでも long-run 実運転では、saved reply の blandness 自体を story / model / prompt 競合込みでさらに下げる課題は残る。

### same-place 判定

現在は process-local な runtime place snapshot ベースの近似であり、厳密な同時刻 occupancy 計算ではない。
会話継続や anomaly 判定に軽い誤差が入りうる。

### failed-turn の durable state

LLM 失敗ターンで発生した移動は process-local snapshot にだけ残り、`character_states` には保存されない。
同じ process 内では session / narration 判定に使われるが、engine 再起動後は DB の committed state へ戻る。

### event_calendar 活用

schema と YAML にはあるが、current runtime での統合は薄い。  
event-driven scene 制御の完成版にはなっていない。

### memory system

`memories` table はあるが、runtime の主要 loop ではまだ中心機能になっていない。

## 3. Web surface

### state source の分離

public viewer の正本は `.dat` であり、local DB と hosted viewer は常に一致するとは限らない。  
`posted_to_web` が残っていたり receiver に失敗すると public 反映が遅れる。

### hosted log retention

`receiver.php` は story ごとの当日 `.dat` に upsert する。
admin の story reset は `config/web_post_targets.local.yaml` に有効な投稿先がある場合、同じ receiver token で hosted 側 `*.dat` の削除も先に依頼する。remote reset が失敗した場合、local DB reset は中断される。

ただし `published` archive は reset 対象外であり、Web 投稿先が未設定の story では hosted 側ログ削除も行われない。

### 公開 API の制約

- `latest` と `replay` が認証不要
- `history` と `characters` は保護されている
- `status.php` の `log_count` は dedupe 後件数ではない
- `config/web_post_targets.local.yaml` は environment-local ファイルであり、story ごとの投稿先メンテナンスは手動運用が残る
- `map_replay` の JS / 画像 asset は hosting 側 cache の影響を受けやすく、deploy 直後に旧 mode が見える場合は hard reload が必要になることがある

## 4. manual と実装のズレ

`docs/users_manual/USER_MANUAL.md` は広くまとまっているが、実装正本ではない。
2026-03-24 時点で scene-aware runtime, review tooling, overlay export/apply, manual PDF 再生成手順について同期を入れるが、特に次の領域は manual より code を優先して読むべきである。

- current model handling
- Web API の公開区分
- `map_replay` と replay asset 構造
- thinking / recovery の実装詳細
- live viewer の実データソース

## 5. 運用上の注意

- `import_story` は進行中 story に対して安全ではない
- `update_story` は guarded field を持つため万能ではない
- smoke 実行は public viewer の確認にはならない
- live viewer の確認では `story_id` の取り違えが起こりやすい
- hosted 側 `.dat` や `published` の残存は local DB reset では消えない
- 一時デバッグ中は `web/config/receiver_debug.log` や hosting 側 `error_log` が証跡として残りうる

## 6. 次の大きな upgrade 候補

実装されていないが、繰り返し議論対象になっている項目は次の通り。

- public API の再整理
- monitor/review の長時間実運転精度の継続改善
- 管理 UI / hosted 運用の整備
- story spec と manual / PDF の自動同期

## 7. 物語増幅レイヤーの不足

2026-03-29 時点の review では、`ankoku_gakuen` のように story YAML / prompt / runtime config を調整すると
局所的な改善は得られる一方、「ユーザーが企図する方向性に沿って、面白さが連鎖的に発火して自己増幅する」
ための低レイヤー機構がまだ薄いことが確認された。

現在の runtime は次を持つ。

- 発話生成
- scene / hook / tension の抽出
- memory / growth / novel への保存

一方で次が不足している。

### 7.1 story intent model

`StoryIntentProfile` 自体は current code に導入済みで、`StoryDirector` の tension / intervention 選好と review / monitor の解釈に使われている。
したがって gap は「intent が無い」ことではなく、intent が planner / canonizer / dramatic pressure 全体にはまだ十分広がっていないことである。

### 7.2 interaction pattern library

persisted `story_interaction_patterns` と deterministic pattern engine は current code に導入済みで、少なくとも次を扱える。

- `misunderstanding`
- `status_clash`
- `bluff_or_showoff`
- `near_reveal`
- `role_reversal`
- `small_win_loss`

したがって current gap は「pattern layer が無い」ことではなく、Phase 2B まで導入済みであることを前提にした高度化へ移っていることである。
特に次は未実装のまま残っている。

- pattern のより rich な evidence source
- pattern の prompt / movement / social target への直接反映
- pattern の semantic quality と cue 設計の改善

2026-05-09 時点では、別層として `ConversationMotifEngine` が追加され、
組み込みの `solo_seed_rondo` は `solo_seed` hook を会話の受け渡しへ弱く反映できる。
ただし自由定義可能な pattern DSL ではなく、管理 UI からオン/オフと強さを切り替える組み込み motif として扱う。

2026-03-31 時点の current code では、`role_reversal` と `bluff_or_showoff` は
relationship mode / canon / active episode を secondary evidence に使えるようになり、
cue 一発依存は少し緩和されている。
ただし real run で dominant pattern として頻出する段階ではまだなく、semantic quality の改善余地は残る。

### 7.3 emergent canonizer

persisted `story_canon_bits` と deterministic `EmergentCanonizer` は current code に導入済みで、
少なくとも次を扱える。

- `pair_dynamic`
- `character_tendency`
- `group_routine`
- `place_motif`

また `momentary_bit -> recurring_bit -> proto_canon -> canon` の昇格段階と、
director / episode / social target への weak bias も current code に入っている。

したがって current gap は canonizer 不在ではなく、次の高度化がまだ残っていることである。

- semantic quality の高い motif 抽出
- profile writeback の強さ / cadence の tuning
- canon から pattern / pressure へ戻る連鎖の live-run tuning

2026-03-31 時点の current code では、prompt 本文への canon summary 注入、`story_hooks` への canon reignition、そして separate `character_canon_overlays` を使った cautious な profile writeback まで導入済みである。
current の主課題は「canon が効かない」ことではなく、live run でどの程度 durable に効かせるかの強さと semantic quality の tuning に移っている。

### 7.4 relationship mode

persisted `relationship_modes` と deterministic `RelationshipModeEngine` は current code に導入済みで、
少なくとも次を扱える。

- `irritated_respect`
- `unsafe_confidant`
- `chaos_partner`
- `cannot_ignore`

また current code では director / episode / social target への weak bias に加え、
同一 pair に cluster ごと最大 2 active modes を持つ richer model、段階 decay、そして complementary mode の
light refresh recovery と persistence diagnostics も導入済みである。

したがって current gap は relationship mode 不在ではなく、次の高度化が残っていることである。

- prompt 本文や発話 contract への richer 反映
- multi-mode persistence と decay の live-run tuning
- mode の semantic richness の改善

### 7.5 episode-level planner

persisted `story_episodes` と `EpisodePlanner` は current code に導入済みで、pattern / hook / tension cluster から
短期 continuity を作り、director / social target / `episode_close` prose に接続されている。

したがって current gap は episode planner 不在ではなく、次の高度化がまだ残っていることである。

- episode progress 判定の精度
- story intent に応じた tempo / close policy の広がり
- relationship mode / canonizer と連動した episode progression

### 7.6 dramatic pressure evaluator

persisted `story_dramatic_pressures` と deterministic `DramaticPressureEvaluator` は current code に導入済みで、
少なくとも次を扱える。

- `status_flashpoint`
- `near_reveal`
- `payoff_ready`
- `stall_risk`

また current code では director ranking / episode opening tie-break / review / probe への接続まで入っている。

したがって current gap は dramatic pressure 不在ではなく、次の高度化が残っていることである。

- prompt 本文への pressure 注入
- social target や movement への pressure 反映
- `role_reversal` / `bluff_or_showoff` 系のより rich な pressure family
- payoff を実際の scene close / intervention へ押し込む強さの調整

2026-03-31 時点の current code では、`showoff_flashpoint` と `role_reversal_ready` が追加され、
scene objective / episode goal への pressure-aware な反映も入っている。
一方で real run の short window では new pressure family が dominant になるところまではまだ確認できておらず、
live 観測窓での出現率と効き方の tuning は残課題である。

### 7.7 文体制御と構造制御の分離不足

現在の改善は prompt と YAML による文体調整の比重が高い。
これは必要だが、構造制御が薄い状態では「軽い文体の平坦な会話」に留まりやすい。

切り分けるべき対象:

- 表層: 文体、語彙、長さ
- 構造: 発話の役割、scene の争点、episode の進展、canon 化

2026-04-09 時点の current code では、visible quality の残差は
`focus missing` や raw fallback volume だけではなく、
「signal は見えているのに bland に着地する kept reply」が主になっている。
したがって current gap は guard 不在ではなく、次の高度化が残っていることである。

- dramatic move の semantic richness
- story / character seed と runtime contract の噛み合わせ
- long-run での blandness 再発率の tuning

### 7.8 current implication

したがって、特定 story を短期的に改善するだけなら story YAML / prompt / runtime config の調整で効果は出る。
ただし、PocketRole を「ユーザーが与えた方向性に沿って、面白さを自己増幅し続ける system」に近づけるには、
上記の中間層を engine の正式概念として持たせる upgrade が必要である。

current の優先候補は次の順で整理する。

1. long-run `visible quality`
2. long-run `growth` commit stability
3. `relationship mode` / `canon writeback` / `dramatic pressure` の live tuning
4. story-quality diagnosis の高度化

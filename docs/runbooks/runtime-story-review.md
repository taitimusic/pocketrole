# Runtime Story Review Runbook

更新日: 2026-04-09

## 目的

実運転や smoke 実行を見て、「モデル問題」なのか「runtime 問題」なのか「story 設定問題」なのかを切り分ける。

## 標準手順

0. `tools.check_stability --v2-closeout` で chapter/director/scene-close の closeout warning を先に確認する
1. `tools.monitor_engine_runtime` で baseline snapshot を取る
2. `tools.generate_story_review_draft` で baseline review draft を生成する
3. `/tmp` copy に対して `tools.runtime_probe_story_events --goal growth` を回す
4. `/tmp` copy に対して `tools.runtime_probe_story_events --goal scene_close` を回す
5. real DB で short から medium の run を回す
6. `tools.monitor_engine_runtime` と `tools.generate_story_review_draft` を再取得する
7. temp DB または real DB で、次を確認する
   - `reply_ratio`
   - `sessionless_monologue_ratio`
   - `active_interaction_patterns`
   - `active_relationship_modes`
   - `complementary_mode_pairs_active`
   - `single_mode_pairs_active`
   - `complementary_mode_reinforcements_recent`
   - `complementary_mode_decay_updates_recent`
   - `complementary_mode_deactivations_recent`
   - `active_canon_bits`
   - `active_dramatic_pressures`
   - `active_episode_type`
   - `active_episode_age`
   - `active_chapter_id`
   - `active_chapter_beat`
   - `active_chapter_age`
   - `active_director_persona_id`
   - `director_satisfaction_overall`
   - `director_satisfaction_tension`
   - `director_satisfaction_pacing`
   - `director_satisfaction_surprise`
   - `episode_close_completion_rate`
   - `closed_scenes_without_arc_recent`
   - `scene_arcs_without_novel_recent`
   - `scene_close_backlog_recent`
   - `scene_close_missing_arc_recent`
   - `scene_close_missing_novel_recent`
   - `scene_close_backlog_recovered_recent`
   - `canon_reignitions_recent`
   - `canon_writebacks_recent`
   - `open_hooks`
   - `active_tensions`
   - `intervention_eligible_now`
   - `growth_candidates_pending`
   - `growth_empty_recent`
   - `growth_quality_rejections_recent`
   - `growth_parse_failures_recent`
   - `growth_rollbacks_recent`
   - `quality_normalizations_recent`
   - `quality_fallbacks_recent`
   - `reply_quality_normalizations_recent`
   - `reply_quality_fallbacks_recent`
   - `reply_focus_misses_recent`
   - `generic_reply_tails_recent`
   - `voice_flat_replies_recent`
   - `signal_visibility_misses_recent`
   - `objective_visibility_misses_recent`
   - `signal_visibility_retry_recent`
   - `objective_visibility_retry_recent`
   - `story_arc`
   - `novel_output`
8. warning が出ている場合は `logs/engine.log` を確認する
9. review draft の `Cause Lens` と `Recommended Experiments` を読み、次に直す slice を 1-2 本に絞る
10. その結果を `docs/system_review/` か `docs/specs/known-gaps.md` に反映する

`upgrade-v2` の closeout 手順をまとめて辿るときは [upgrade-v2-closeout.md](./upgrade-v2-closeout.md) を正本として使う。

## current final-eval readout

2026-04-03 の `ankoku_gakuen` final eval を current truth の例とする。

- real DB after:
  - `reply_ratio=0.70`
  - `sessionless_monologue_ratio=0.07`
  - `growth_commits_recent=39`
  - `quality_fallbacks_recent=37`
  - `reply_quality_fallbacks_recent=29`
  - `growth_parse_failures_recent=14`
- `/tmp` growth probe:
  - `goal_reached=true`
  - `growth_commit_delta=1`
- `/tmp` scene_close probe:
  - `goal_reached=false`
  - `scene_arc_delta=0`
  - `novel_output_delta=0`

このため current loop では、
`growth` が動いているか、`scene_close` が longer-run でまだ弱いか、`visible quality` が residual problem かを
before/after + probe の 3 点で読む。

## まず疑う順番

1. runtime feature flag が有効か
2. quality gate suppression が通常保存を潰していないか
3. conversation pressure / social gravity が会話を作れているか
4. pattern / relationship mode / canon / pressure / episode continuity が立っているか
5. scene close が `closed_only` / `arc_created` / `complete` のどこで止まっているか
6. chapter pressure が scene close / hook resolve / intervention へ変換されているか
7. director satisfaction の低い axis が tension / pacing / surprise のどこに出ているか
8. growth / novel が後段で失速していないか
9. そのうえで story YAML や prompt を疑う

## Cause Lens の読み方

`Gaps` は症状、`Cause Lens` は上位原因として扱う。

- `signal_visibility_weak`
  - signal layer は立っているが、reply が返し先やキャラ差として見えておらず、dominant signal や scene objective の争点も表面化していない
- `scene_purpose_not_visible`
  - scene objective や payoff push が persisted state にある割に台詞や close に出ていない
- `quality_gate_suppression`
  - normalize / retry / fallback が高く、通常生成が保存まで届いていない
- `chapter_pressure_not_converting`
  - active chapter はあるが、beat progress が scene close / hook resolve / intervention へ変換されていない
- `director_alignment_low`
  - director satisfaction が低く、story intent と runtime 配分が噛み合っていない
- `canon_carryover_weak`
  - canon はあるが reignition / writeback / canon-triggered hook が弱い
- `growth_not_sticking`
  - growth evidence はあるが empty / rejection / pending accumulation が先に立つ
- `turnflow_weak`
  - reply ratio / sessionless monologue / all-chars-spoke が flat 寄り

## warning の読み順

1. `engine_stopped` / `turn_stalled` / `ollama_failed`
2. `reply_quality_fallback_high`
3. `reply_quality_flat`
4. `scene_close_weak`
5. `relationship_mode_flat`
6. `chapter_progress_stalled`
7. `director_satisfaction_low`

`reply_quality_fallback_high` がある場合は、まず fallback volume を下げる。`reply_quality_flat` だけが残る場合は focus/voice の surface 問題として扱う。`signal_visibility_weak` が review 上位なら `signal_visibility_misses_recent` と `objective_visibility_misses_recent` のどちらが大きいかを先に見る。`scene_close_weak` が出た場合は `scene_close_missing_arc_recent` と `scene_close_missing_novel_recent` のどちらが dominant か、`scene_close_backlog_recovered_recent` が増えているかを先に見る。読み順は `closed_only -> arc_created -> complete` で固定し、`missing_arc` が多いなら context rebuild 側、`missing_novel_output` が多いなら existing arc reuse 側を先に疑う。特に `arc_created` が続く場合は、existing arc の title / summary が prose handoff と deterministic fallback に戻っているかを確認する。`relationship_mode_flat` が出た場合は `complementary_mode_pairs_active` が 0 かを先に見て、そのうえで `complementary_mode_reinforcements_recent` と `complementary_mode_deactivations_recent` のどちらが優勢かで persistence 崩れか refresh 不足かを切り分ける。`chapter_progress_stalled` と `director_satisfaction_low` が同時に出る場合は、chapter handoff と director axis のどちらが先に崩れているかを review draft の `Cause Lens` で切り分ける。
`missing_arc` を疑うときは、scene 自体が閉じているかに加えて、`scene_outcome_summary`・`participant_names`・`source_scene_id` の handoff rebuild が揃っているかを先に確認する。

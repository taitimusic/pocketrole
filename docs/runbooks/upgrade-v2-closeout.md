# Upgrade V2 Closeout Runbook

更新日: 2026-04-11

## 目的

Chapter System / Director Persona を含む `upgrade-v2` を「実装済み」ではなく
「実運転で閉じられる状態」まで確認するための標準手順を固定する。

対象は次の残課題である。

- long-run で chapter / director が停滞しないか
- `scene_close` / `novel_output` の handoff が追いつくか
- visible quality の残差が monitor / review で読めるか

## 今日やる 3 件

1. `check_stability --v2-closeout` で closeout warning を 1 コマンドで確認する
2. `monitor_engine_runtime` と `generate_story_review_draft` で long-run の症状と原因を固定フォーマットで読む
3. `/tmp` probe と short/medium real run を併用し、`scene_close` と `growth` の handoff を spot-check する

## 標準コマンド

### 1. baseline sanity

```bash
cd pocketrole
.venv/bin/python -m tools.check_stability --story <story_id> --v2-closeout --window-turns 40
```

このコマンドは次をまとめて warning 化する。

- `chapter_progress_stalled`
- `director_satisfaction_low`
- `scene_close_weak`
- `reply_quality_residual`

### 2. monitor snapshot

```bash
.venv/bin/python -m tools.monitor_engine_runtime \
  --story <story_id> \
  --engine-pid <pid> \
  --interval-sec 15 \
  --max-entries 4 \
  --window-turns 40
```

### 3. review draft

```bash
.venv/bin/python -m tools.generate_story_review_draft \
  --story <story_id> \
  --window-turns 40
```

### 4. `/tmp` probe

```bash
.venv/bin/python -m tools.runtime_probe_story_events --story <story_id> --goal growth
.venv/bin/python -m tools.runtime_probe_story_events --story <story_id> --goal scene_close
```

### 5. short/medium real run

- Chapter / Director を有効にした config で 20-40 turn 程度回す
- run 後に 1-3 を再実行する

## Closeout Gate

### 必須

- `check_stability --v2-closeout` で `chapter_progress_stalled` が出ない
- `check_stability --v2-closeout` で `director_satisfaction_low` が出ない、または review で原因と次手が特定できている
- `scene_close_completion_rate >= 0.50` を monitor / review / closeout check のいずれでも確認できる
- review draft の `Cause Lens` が `chapter_pressure_not_converting` / `director_alignment_low` / `quality_gate_suppression` のどれかに収束して読める

### 準必須

- `reply_quality_fallbacks_recent` が縮小傾向である
- `reply_focus_misses_recent` と `voice_flat_replies_recent` が monitor 上で支配的 warning になっていない
- `/tmp` probe で `growth` は継続して達成し、`scene_close` の stage が `closed_only` に留まり続けない

## 読み順

1. `check_stability --v2-closeout`
2. `monitor_engine_runtime` の warning
3. `generate_story_review_draft` の `Gaps` と `Cause Lens`
4. `runtime_probe_story_events --goal scene_close`
5. `runtime_probe_story_events --goal growth`

## 典型パターン

### `chapter_progress_stalled` + `director_satisfaction_low`

- chapter handoff と director axis の両方を見る
- 先に review draft の `Cause Lens` で `chapter_pressure_not_converting` か `director_alignment_low` を切り分ける

### `scene_close_weak`

- `scene_close_missing_arc_recent` と `scene_close_missing_novel_recent` のどちらが dominant かを見る
- `closed_only -> arc_created -> complete` の順で止まり方を判断する

### `reply_quality_residual`

- fallback volume が主因か、保存後の平板さが主因かを monitor の内訳で切り分ける
- `reply_focus_misses_recent` が主因なら focus 系
- `voice_flat_replies_recent` が主因なら saved reply の運び方

## Closeout 記録

closeout できた run では、最低限次を残す。

- 実行日
- story_id
- 使用 model / provider
- `check_stability --v2-closeout` の結果
- monitor warning の有無
- review draft の上位 `Cause Lens`
- `scene_close` / `growth` probe の結果

保存先は `docs/system_review/` または別途 closeout メモでよいが、tag を切る前に必ず残す。

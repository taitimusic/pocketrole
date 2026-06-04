# Story Tuning Loop

更新日: 2026-04-04

## 目的

特定 story の出力品質を上げたいときに、設定調整で効く範囲と engine 側の gap を混同しないための runbook。

## 進め方

1. `docs/specs/story-quality-playbook.md` を見て、症状を分類する
2. `docs/specs/known-gaps.md` を見て、engine gap に該当しないか確認する
3. setting / prompt 側で直す場合は、次だけを先に触る
   - キャラの役割分担
   - scene の争点
   - relationship の歪み
   - hook を立てやすい発話
4. runtime で直すべき場合は、`docs/specs/known-gaps.md` か issue に次の修正案を記録する
5. real DB short run で visible quality を確認し、post-round truth は `/tmp` copy の `runtime_probe_story_events` で切り分ける
6. visible quality が複数 slice 後も `reply_ratio` / `sessionless_monologue_ratio` は良いのに
   `reply_quality_fallbacks_recent` / `reply_focus_misses_recent` だけ高止まりする場合は、
   story / model / long prompt 競合の限界が混ざっているとみなし、深追いを止めて final evaluation へ進む
7. final evaluation と docs sync を終えたら、次の upgrade へ入る前に
   `docs/runbooks/stable-tag-and-upgrade-branch.md` の手順で stable tag を打ち、baseline を固定する

## 判断基準

- 局所 tone や会話の短さは、story YAML / prompt 調整で改善しやすい
- 面白さの連鎖、偶発ネタの再利用、episode 単位の進展は engine 側 upgrade が必要
- current engine には `pattern / relationship mode / canon / pressure / episode / canon writeback` が既にあるので、残りは long-run tuning と story-quality diagnosis を中心に疑う
- 2026-04-03 final eval 時点では、`ankoku_gakuen` は `reply_ratio=0.70` と `sessionless_monologue_ratio=0.07` までは改善済みなので、
  その先の visible quality 残差は「直せるバグ」と「runtime / model 限界」が混ざっている前提で読む

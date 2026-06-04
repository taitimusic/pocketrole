# Stable Tag And Upgrade Branch

更新日: 2026-04-04

## 目的

ひとつの upgrade が一段落した時点で、その完成版を壊さず保存しつつ、同じ repo / 同じディレクトリのまま次の upgrade へ進むための標準手順を残す。

## いつ使うか

- final evaluation と docs sync が終わったあと
- 次の upgrade を始めたいが、現行版の復元点も残したいとき
- repo に一時生成物や未整理ファイルがあり、tag 前に safe cleanup を入れたいとき

## 原則

- `stable tag` は dirty worktree ではなく、復元基準にしたい commit へ直接打つ
- `branch` は Git の履歴上の分岐であり、新しい作業ディレクトリやファイル移動を意味しない
- 一時生成物だけを cleanup し、保存価値の判定が必要な untracked files は自動削除しない
- 並行作業で別ディレクトリが必要な場合だけ `git worktree` を使う

## 標準手順

1. final evaluation と docs sync が終わった stable commit を決める
2. repo 内のノイズを棚卸しし、safe cleanup 対象を限定する
3. stable commit に annotated tag を打つ
4. dirty worktree に残っている tracked / untracked files を分類する
5. 次の upgrade 用 branch を stable commit から切る
6. 必要なら `git worktree` で別ディレクトリへ展開する

## safe cleanup の考え方

### その場で消してよいことが多いもの

- `__pycache__/`
- `.pytest_cache/`
- preview 用の一時 PNG や `/tmp` 相当の scratch output
- 再生成前提の局所 cache

### 自動削除しないもの

- `docs/system_review/`, `docs/monitoring/` などの観測 artifact
- untracked migration, tool, test, engine file
- 画像、音声、story map asset
- agent 作業資産や local-only の scratch ディレクトリ
- local config や手元の検証メモ

これらは `keep and commit / archive outside repo / .gitignore へ寄せる / delete` を個別に決める。

## 推奨コマンド

stable commit を確認する。

```bash
git rev-parse <stable-commit>
```

safe cleanup 前に worktree を確認する。

```bash
git status --short
git ls-files --others --exclude-standard
```

stable commit に annotated tag を打つ。

```bash
git tag -a <tag-name> <stable-commit> -m "<tag message>"
```

次の upgrade branch を stable commit から作る。

```bash
git switch -c <upgrade-branch> <stable-commit>
```

別ディレクトリで並行作業したい場合だけ `worktree` を使う。

```bash
git worktree add ../pocketrole-next <upgrade-branch>
```

## current 参考例

2026-04-04 時点では、次の baseline を stable tag として保存した。

- stable commit: `98b9fb0`
- tag: `v1-upgrade-complete-2026-04-03`

この基準点は、current completed upgrade の tracked repo 状態を復元するために使う。

## 注意

- tag だけでは untracked files や未コミット変更は復元できない
- dirty worktree の内容まで残したい場合は、別 branch への commit、`git stash`, repo 外 archive のいずれかが別途必要
- 次 upgrade の開始前に `git status --short` が大きく汚れている場合は、branch を切る前に分類だけでも済ませる

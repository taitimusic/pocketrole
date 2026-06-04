# Release Readiness Checklist

更新日: 2026-05-02

## 目的

PocketRole を配布可能な状態として固定する前に、実装、初回導線、配布物、local secret、タグ付け準備を一通り確認する。

この checklist は次の runbook の入口である。

- `docs/runbooks/fresh-install-smoke-test.md`
- `docs/runbooks/upgrade-v2-closeout.md`
- `docs/runbooks/stable-tag-and-upgrade-branch.md`

## 判定の考え方

配布前の完了は「全機能が未来永劫完成」ではなく、次を満たす状態とする。

1. 新規ユーザーが sample story を起動し、local viewer で成功を確認できる
2. sample story から custom story へ移る admin 導線がある
3. 公開 viewer は local first の次段階として説明されている
4. local secret / DB / 個人環境メモが配布物に混ざっていない
5. stable tag を打てる clean tracked state がある

## 1. Git 状態

確認コマンド:

```bash
git status --short
git log --oneline -5
```

Pass:

- 配布対象の tracked diff は commit 済み
- 未追跡ファイルは配布対象か個人環境ファイルか分類済み
- `.codex` や個人 PC の network diagnosis は commit 対象に含めない

Fail:

- 実装差分が未コミットのまま残っている
- 配布対象か判断できない untracked file がある
- secret や local DB が tracked diff に入っている

## 2. Fresh Install Smoke

実行:

```bash
docs/runbooks/fresh-install-smoke-test.md
```

Pass:

- OpenAI API route で `ankoku_gakuen` を起動できる
- `/tmp` の smoke DB に `chat_logs` が生成される
- admin local viewer で表情画像つき会話が見える
- onboarding / authoring 導線へ進める

Fail:

- `START_HERE.md` の通りに進めても local viewer まで到達できない
- admin credential guard と docs の起動手順がずれている
- sample story の画像や map asset が欠けている

## 3. Regression

最低限:

```bash
cd pocketrole
.venv/bin/pytest tests/test_story_onboarding.py tests/test_admin_service.py -q
```

広めの確認:

```bash
cd pocketrole
.venv/bin/pytest \
  tests/test_story_onboarding.py \
  tests/test_admin_service.py \
  tests/test_update_story.py \
  tests/test_validate_story.py \
  tests/test_db_manager.py \
  -q
```

Pass:

- onboarding / admin authoring の focused regression が通る
- story update / validation / DB manager を含む広めの regression が通る

Fail:

- admin authoring の保存 API が失敗する
- sample story validation が失敗する
- DB migration / import / update が壊れている

## 4. Upgrade V2 Runtime Closeout

実行:

```bash
docs/runbooks/upgrade-v2-closeout.md
```

Pass:

- `check_stability --v2-closeout` の warning が許容範囲
- chapter / director / scene close / growth の観測結果が記録されている
- review draft の `Cause Lens` で残課題と次手を説明できる

Fail:

- chapter / director が long-run で停止する
- visible quality residual が会話本文に残る
- warning の原因が説明できない

## 5. Docs / PDF

確認対象:

- `README.md`
- `START_HERE.md`
- `docs/users_manual/USER_MANUAL.md`
- `docs/users_manual/USER_MANUAL.pdf`
- `AGENT_GUIDE.md`
- `docs/runbooks/`

確認コマンド例:

```bash
pdftotext docs/users_manual/USER_MANUAL.pdf - | rg "local viewer|clone wizard|admin/admin|chapter / beat / beat event"
git diff --check
```

Pass:

- Markdown と PDF が同じ更新内容を含む
- admin 起動手順が credential guard と一致している
- `START_HERE.md` は OpenAI API first の最短導線になっている
- public viewer は local first の後段として説明されている

Fail:

- PDF が古いまま
- `admin/admin` のまま起動できるように見える説明が残る
- Ollama 導入が初回成功の必須手順に見える

## 6. 配布物除外

commit / tag 前に、次が tracked diff に入っていないことを確認する。

- `.env`
- `config/llm_runtime.local.yaml`
- `config/web_post_targets.local.yaml`
- `pocketrole/db/*.db`
- `.codex`
- 個人 PC の network diagnosis
- 一時生成画像、ローカル session artifact

確認コマンド:

```bash
git status --short
git ls-files --others --exclude-standard
```

公開用 GitHub リポジトリを新規履歴で作る場合は、repo root で次を実行する。

```bash
tools/scripts/build_public_release.py --version 0.1.0 --force
```

生成先は `pocketrole_for_github/version_0_1_0/` とし、親 repo では `.gitignore` により管理対象外にする。生成フォルダは `git init` 済みなので、このディレクトリから GitHub に push する。

## 7. Stable Tag

上記が pass したら、次を実行する。

```bash
docs/runbooks/stable-tag-and-upgrade-branch.md
```

推奨タグ名の例:

```text
v2-upgrade-complete-2026-05-02
```

タグ前に確認すること:

- tag 対象 commit SHA
- release readiness の実行日
- fresh install smoke の結果
- regression の結果
- closeout 記録の保存先

## Release Note に残す要点

- OpenAI API first の初回導線
- admin local viewer
- sample story clone / custom authoring
- character / place / story / event / anomaly / director / chapter 編集
- chapter runtime controls
- LLM runtime profile settings
- fresh install smoke result
- known residual risks

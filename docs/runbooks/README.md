# PocketRole Runbooks

更新日: 2026-05-02

## 目的

このディレクトリは、PocketRole の runtime 観測、story tuning、review loop の手順を残すための置き場である。
設計そのものは `docs/specs/` を正本とし、ここでは「どう回すか」を扱う。

## current runbooks

- [fresh-install-smoke-test.md](./fresh-install-smoke-test.md)
  - 配布前に、新規ユーザーの最短導線で local viewer まで到達できるか確認する手順
- [release-readiness-checklist.md](./release-readiness-checklist.md)
  - fresh install smoke、regression、docs/PDF、配布除外、stable tag 前の総合 checklist
- [runtime-story-review.md](./runtime-story-review.md)
  - monitor、review draft、smoke、temp DB 確認の標準手順
- [story-tuning-loop.md](./story-tuning-loop.md)
  - story YAML / prompt 調整と engine gap の切り分け手順
- [stable-tag-and-upgrade-branch.md](./stable-tag-and-upgrade-branch.md)
  - 完成版を stable tag で固定し、safe cleanup のあと次の upgrade branch へ進む手順
- [upgrade-v2-closeout.md](./upgrade-v2-closeout.md)
  - Chapter / Director を含む upgrade-v2 の実運転 closeout 手順

# PocketRole 実装仕様書群

更新日: 2026-03-31

## 目的

このフォルダは、PocketRole の現行実装を次の AI コーディング作業向けに整理した実装仕様書群である。  
利用者向けの説明ではなく、次にコードを触る開発者やエージェントが「今の system がどう動いているか」を誤解なく掴むことを目的とする。

## この文書群の正本範囲

このフォルダの内容は、次の順で正本とみなす。

1. Python / PHP / SQL の現在実装
2. 現在の DB スキーマと migration
3. 現在の live 検証結果と運用観測
4. 既存 README のうち manual / proposal / bug log

`docs/users_manual/USER_MANUAL.md` は利用ガイドであり、実装仕様の正本ではない。
manual と code が食い違う場合は、まず code と live 挙動を優先し、その差分は `known-gaps.md` に記録する。

## 文書一覧

- [system-overview.md](./system-overview.md)
  - アプリ全体の目的、主要コンポーネント、story-emergence runtime 層、local DB と hosted public/admin 面の分離をまとめる。
- [runtime-architecture.md](./runtime-architecture.md)
  - `engine.main` 起点の起動、story ごとの Web 投稿設定解決、`config/llm_runtime.local.yaml` による runtime LLM 切替、relationship mode / canonizer / dramatic pressure / episode planner を含む post-round runtime、monitor / review / probe tooling をまとめる。
- [simulation-engine.md](./simulation-engine.md)
  - `StoryEngine` の legacy path と scene-aware path、reply-first 会話制御、social gravity、quality normalization/fallback、signal-aware prompt、hook/pattern/episode/growth/director、scene-close / episode-close prose まで含めた current behavior をまとめる。
- [data-model.md](./data-model.md)
  - YAML seed、SQLite、runtime overlay、chat log / scene / hook / pattern / relationship mode / canon / pressure / episode / growth 系 state のデータ構造と更新ルールをまとめる。
- [web-surface.md](./web-surface.md)
  - `viewer.php`, `map_replay.php`, `api.php`, `receiver.php`, `status.php`, `archive.php`, `archive_index.php` の公開面と JSONL / published archive 仕様をまとめる。
- [hosted-admin-surface.md](./hosted-admin-surface.md)
  - `aiohttp` 管理 UI、管理 API、token CLI、publish loop、hosted 配備契約をまとめる。
- [story-lifecycle-and-tools.md](./story-lifecycle-and-tools.md)
  - story authoring, validation, import, update, story map scaffold, Web 投稿設定初期化, smoke, monitor, runtime probe, growth overlay export/apply, review draft までの運用フローをまとめる。
- [known-gaps.md](./known-gaps.md)
  - 現在の制約、既知課題、manual との差分、今後の検討余地をまとめる。
- [story-quality-playbook.md](./story-quality-playbook.md)
  - story 性と出力品質を高めるための実践ガイド。runtime flag, seed 設計, quality gate, monitor 指標, canon/growth/live-run tuning の見直し観点をまとめる。

## 周辺ディレクトリの役割

- [../plans/](../plans/)
  - 未実装だが実装可能な設計案の置き場。future architecture や slice 設計はここへ書く。
- [../decisions/](../decisions/)
  - 採用した方針の短い正本。長文 spec ではなく、判断と理由を固定したいときに使う。
- [../runbooks/](../runbooks/)
  - 実運転、review、story tuning の手順書。日常運用の標準手順はここへ寄せる。
- [../system_review/](../system_review/)
  - 特定 story や特定期間の観測結果。現象の記録であり、仕様の正本ではない。

参照順は、`CLAUDE.md` -> `docs/specs/README.md` -> 関連 spec -> `known-gaps.md` -> `plans/` -> `runbooks/` を基本とする。

## 既存 README との役割分担

### 実装正本ではないが重要な文書

- [../users_manual/USER_MANUAL.md](../users_manual/USER_MANUAL.md)
  - 利用手順と運用方法のまとめ。実装差分がある可能性がある。
- [../BUG_FIX.md](../BUG_FIX.md)
  - 障害と修正履歴の台帳。
- [../MODEL_OUTPUT_COMPARISON.md](../MODEL_OUTPUT_COMPARISON.md)
  - 実モデル比較の結果と採用判断。

### 将来提案・方針メモ

- [../WEB_API_VISIBILITY_PROPOSAL.md](../WEB_API_VISIBILITY_PROPOSAL.md)
- [../HOSTED_SERVICE_REFERENCE.md](../HOSTED_SERVICE_REFERENCE.md)
- [../AUTONOMOUS_MYSTERY_UPGRADE_PLAN.md](../AUTONOMOUS_MYSTERY_UPGRADE_PLAN.md)

これらは future plan であり、現行仕様の正本ではない。

## 更新ルール

- 仕様変更より先に code が変わった場合は、このフォルダを code に追随させる。
- proposal を採用しても、実装が入るまでは current spec に書かない。
- live 検証で「実装通りに動かない」ことが分かった場合は、`known-gaps.md` にまず記録する。
- story 固有の例は必要最小限に留め、共通構造を優先して書く。

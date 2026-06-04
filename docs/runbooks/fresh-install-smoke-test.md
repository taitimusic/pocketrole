# Fresh Install Smoke Test Runbook

更新日: 2026-05-02

## 目的

配布前に、新規ユーザーが最短ルートで初回成功まで到達できるかを確認する。

成功条件は次の 3 つである。

1. `ankoku_gakuen` を OpenAI API profile で起動できる
2. local DB に `chat_logs` が生成される
3. admin local viewer で表情画像つきの会話を確認できる

この runbook は公開 Web 投稿の確認ではない。公開 viewer / receiver の確認は、local first の成功後に別途行う。

## 前提

- Python 3.11+ が使える
- OpenAI API key を用意できる
- repo root から作業する
- 既存の運用 DB を壊さないため、検証用 DB は `/tmp` に分離する

以降の例では repo root を `pocketrole` とする。

## 1. 依存関係を準備する

```bash
cd pocketrole
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

確認:

- `.venv/bin/python` が存在する
- install がエラーで止まらない

## 2. local 設定を用意する

```bash
cp config.yaml.example config.yaml
cp config/llm_runtime.example.yaml config/llm_runtime.local.yaml
```

`.env` に最小の secret を入れる。

```env
OPENAI_API_KEY=sk-your-key
```

`config/llm_runtime.local.yaml` の `active_profile` を OpenAI の低コスト profile にする。

```yaml
active_profile: "openai_nano"
```

確認:

- `.env` に `OPENAI_API_KEY=` がある
- `config/llm_runtime.local.yaml` に `active_profile: "openai_nano"` がある

## 3. sample story を検証する

```bash
.venv/bin/python -m tools.validate_story stories/ankoku_gakuen/
```

確認:

- validation が成功する

## 4. 検証用 DB に import する

既存の `db/pocketrole.db` を触らず、fresh install smoke 用の DB を `/tmp` に置く。

```bash
SMOKE_DB="/tmp/pocketrole-fresh-install-smoke.db"
.venv/bin/python -m tools.import_story stories/ankoku_gakuen/ --db "$SMOKE_DB"
```

確認:

- `$SMOKE_DB` が作られる
- import が成功する

注意:

- fresh install smoke では `--force-replace` を使わない
- 既存の運用 DB で smoke しない

## 5. engine を短時間起動する

```bash
SMOKE_DB="/tmp/pocketrole-fresh-install-smoke.db"
.venv/bin/python -m engine.main --stories ankoku_gakuen --db "$SMOKE_DB"
```

数ターン進んだら停止する。

確認:

- engine が即時クラッシュしない
- OpenAI API の認証エラーが出ない
- `logs/engine.log` に turn が出る

## 6. chat log を確認する

```bash
sqlite3 /tmp/pocketrole-fresh-install-smoke.db \
  "select turn_number, speaker_name, substr(message,1,80) from chat_logs where story_id='ankoku_gakuen' order by id desc limit 10;"
```

成功の目安:

- 1 件以上の発話がある
- `speaker_name` と `message` が空でない

## 7. admin local viewer を確認する

admin も同じ smoke DB を読む設定で起動する。
admin は初期値の `admin/admin` では起動しないため、smoke 用の資格情報を明示する。

```bash
SMOKE_DB="/tmp/pocketrole-fresh-install-smoke.db"
POCKETROLE_ADMIN_USER="smoke_admin" \
POCKETROLE_ADMIN_PASSWORD="change-me-for-smoke" \
POCKETROLE_ADMIN_COOKIE_SECURE=0 \
.venv/bin/python -m admin.main --db "$SMOKE_DB"
```

ブラウザで開く。

```text
http://127.0.0.1:8787/admin/viewer/ankoku_gakuen
```

成功の目安:

- キャラ名と発話が時系列で見える
- 表情画像が表示される
- turn / place / runtime state が見える

## 8. clone / authoring 導線を確認する

local viewer が見えたら、次を開く。

```text
http://127.0.0.1:8787/admin/onboarding/ankoku_gakuen
```

確認:

- sample story を複製する wizard が開く
- clone 後の story で、キャラ、場所、story 基本設定、イベント/異変、director persona、chapter / beat / beat event の編集導線へ進める

## 判定

### Pass

- validation が通る
- import が通る
- engine が 1 turn 以上生成する
- `chat_logs` に発話が入る
- admin local viewer で表情画像つき会話が見える
- onboarding / authoring 導線を開ける

### Fail

次のどれかがあれば配布前に修正する。

- Quick Start 通りに依存関係を入れられない
- OpenAI profile の設定が docs と一致しない
- sample story validation が失敗する
- engine が起動直後に停止する
- local DB に発話が入らない
- local viewer が 500 / blank になる
- 表情画像が表示されない

## 記録

配布前 smoke では、最低限次を残す。

- 実行日
- commit SHA
- Python version
- active LLM profile
- smoke DB path
- generated turn count
- admin local viewer の確認結果

保存先は `docs/system_review/` または release note でよい。

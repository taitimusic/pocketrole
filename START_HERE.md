# PocketRole Start Here

更新日: 2026-05-02

このソフトのいちばん短い使い方は、まずサンプル story を動かして、キャラ同士の会話が出るところまで確認することです。

PocketRole で最初に体験してほしい価値は次です。

- 自作キャラ同士にやり取りをさせられる
- その結果としてストーリーが自然に出てくる
- 出てきた会話や流れを、小説や設定づくりの素材として流用できる

公開 viewer や Web 投稿はその次です。最初はローカルで会話生成が動くところまでで十分です。

## 最初のゴール

最初の成功条件は次です。

1. `ankoku_gakuen` を起動する
2. キャラの会話が生成される
3. admin の local viewer で表情画像つきの会話を確認する

ここまで到達したら、その次に自作キャラや自作 story へ差し替えればよい、という順番で進めてください。

## まず AI エージェントを案内役に使う

Codex や Claude Code を使えるなら、最初に次を渡すのがいちばん楽です。

```text
この repo の新規ユーザー導入を手伝ってください。
まず START_HERE.md と README.md を読み、PocketRole を OpenAI API で最短起動したいです。
ゴールは ankoku_gakuen をローカルで動かして、会話ログが生成されるところまでです。
可能なら admin の local viewer で表情画像つきの会話表示まで確認してください。
危険な DB 初期化や force-replace は勝手に行わず、各手順の確認結果を見せながら進めてください。
```

AI エージェントを使う場合でも、下の手順を順番どおりに追えば手動で進められます。

## 最短ルート: OpenAI API でまず一回動かす

このルートは、Ollama をまだ入れていない人向けです。
API 利用料金はかかりますが、初回成功体験まではいちばん短くなります。

作業ディレクトリ:

```bash
cd pocketrole
```

### 1. 仮想環境を作る

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

確認:

- エラーなく依存が入る

### 2. ローカル設定ファイルを作る

```bash
cp config.yaml.example config.yaml
cp config/llm_runtime.example.yaml config/llm_runtime.local.yaml
```

確認:

- `config.yaml`
- `config/llm_runtime.local.yaml`

の 2 ファイルができている

### 3. `.env` に OpenAI API key を入れる

この repo には `.env.example` はありません。必要な変数だけ書きます。

最小例:

```env
OPENAI_API_KEY=sk-your-key
```

注意:

- 引用符は付けない
- 行末コメントを付けない
- まずは OpenAI だけでよい

確認:

- `.env` に `OPENAI_API_KEY=` がある

### 4. runtime を OpenAI に切り替える

`config/llm_runtime.local.yaml` を開いて、`active_profile` を `openai_nano` にします。

例:

```yaml
active_profile: "openai_nano"
```

そのまま example の `profiles` を使えば、モデルは `gpt-5.4-nano` です。

確認:

- `active_profile: "openai_nano"` になっている

### 5. story を検証する

```bash
.venv/bin/python -m tools.validate_story stories/ankoku_gakuen/
```

確認:

- validation が成功する

### 6. story を DB に投入する

```bash
.venv/bin/python -m tools.import_story stories/ankoku_gakuen/ --db db/pocketrole.db
```

確認:

- `db/pocketrole.db` が作られる
- import が成功する

注意:

- 新規導入では `import_story`
- 進行中 story の更新では `update_story`
- `--force-replace` は最初の導線では使わない

### 7. engine を起動する

```bash
.venv/bin/python -m engine.main --stories ankoku_gakuen
```

確認:

- `logs/engine.log` に新しい turn が出る
- エラーで即終了しない

### 8. まずログで会話生成を確認する

まずはログか DB で、engine が実際に会話を生成していることを確認します。

ログ確認:

```bash
tail -f logs/engine.log
```

別ターミナルで DB を見る例:

```bash
sqlite3 db/pocketrole.db "select turn_number, speaker_name, substr(message,1,60) from chat_logs where story_id='ankoku_gakuen' order by id desc limit 10;"
```

成功の目安:

- `chat_logs` が増えている
- キャラ名付きの発話が入っている
- 1ターンで止まらず数ターン進む

### 9. admin の local viewer で読む

ログで生成を確認できたら、次はブラウザで local viewer を開きます。

admin 起動例:

```bash
POCKETROLE_ADMIN_USER="local_admin" \
POCKETROLE_ADMIN_PASSWORD="change-me-before-sharing" \
POCKETROLE_ADMIN_COOKIE_SECURE=0 \
.venv/bin/python -m admin.main
```

ブラウザで開く場所:

```text
http://127.0.0.1:8787/admin
```

上の例では、ログインユーザーは `local_admin`、パスワードは `change-me-before-sharing` です。
admin service は `admin/admin` のままでは起動しません。

ログイン後、`ankoku_gakuen` の詳細から `viewerを開く` を選ぶか、次を直接開きます。

```text
http://127.0.0.1:8787/admin/viewer/ankoku_gakuen
```

成功の目安:

- キャラ名と発話が時系列で見える
- 表情画像が表示される
- turn / 場所 / runtime 状態が見える

この local viewer は `db/pocketrole.db` を見ています。公開用の `viewer.php` とは別です。

### 10. サンプルから自作 story を作り始める

`ankoku_gakuen` が見えたら、次は clone wizard で自分用 story を作れます。

```text
http://127.0.0.1:8787/admin/onboarding/ankoku_gakuen
```

最初に差し替えられる項目:

- 新しい `story_id`
- タイトル
- 説明
- キャラ名
- キャラの短い説明
- goal / worry
- キャラごとの `neutral.png`

clone 後の story では、admin から次の編集にも進めます。

- キャラ基本情報、追加、削除、ID rename
- 10表情画像
- 場所基本情報、追加、削除、ID rename、隣接関係
- story 基本設定、実行設定、LLM runtime profile
- イベント/異変、condition builder
- director persona
- chapter / beat / beat event

細かい調整や一括編集では YAML と運用コマンドも引き続き使えますが、初回の差し替え作業は admin の編集導線から進められます。

## よくあるつまずき

### OpenAI API key を入れたのに動かない

まず確認するもの:

- `.env` に `OPENAI_API_KEY` があるか
- 値に引用符や余計な空白が入っていないか
- `config/llm_runtime.local.yaml` の `active_profile` が `openai_nano` か

### engine は動いたが public viewer に何も出ない

これは初回導線では正常です。
公開用 Web viewer は別設定です。`receiver.php` への投稿設定をしていなければ公開側には出ません。

まずは admin の local viewer を確認してください。

```text
http://127.0.0.1:8787/admin/viewer/ankoku_gakuen
```

### Ollama の説明が見えるが、今すぐ必要か

不要です。最初は OpenAI API だけで進めてください。
Ollama は動作確認後にコストを抑えるための次のルートです。

## 次の一歩

初回成功の次は、この順で進めるのが分かりやすいです。

1. **local viewer で読み味を確認する**
   表情画像つきで、生成された会話がどう見えるか確認する
2. **clone wizard で自作 story を作り始める**
   `ankoku_gakuen` を複製して、自分のキャラ名や説明へ差し替える
3. **Ollama に切り替える**
   ローカル LLM に移して、コストを抑えながら試行回数を増やす
4. **Web viewer / 公開設定を足す**
   ローカル動作が安定してから公開面を追加する

## どの文書を次に読むか

- 概要を見直す: `README.md`
- 運用の全体手順を見る: `docs/users_manual/USER_MANUAL.md`
- 配布前の初回導線 smoke を見る: `docs/runbooks/fresh-install-smoke-test.md`
- AI エージェントに安全運用まで任せる: `AGENT_GUIDE.md`

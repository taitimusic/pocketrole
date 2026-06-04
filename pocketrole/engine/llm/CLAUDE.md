# engine/llm/ — LLM プロバイダーレイヤー

## モジュール境界

LLM プロバイダーの抽象化と concurrency 制御を担う。
エンジン本体はこのディレクトリの具象クライアントを直接使わず、`LLMRouter` 経由で呼ぶ。

## 構成

- `base.py` — `BaseLLMClient`（抽象基底）+ `LLMResponse`（応答 DTO）
- `router.py` — `LLMRouter`（provider 振り分け + キュー/セマフォ管理）
- `exceptions.py` — `LLMError`, `LLMTimeoutError`, `LLMRateLimitError` 等
- `ollama_client.py` — Ollama（ローカル、直列処理）
- `openai_client.py` — OpenAI（クラウド、api_mode で endpoint 自動振り分け）
- `anthropic_client.py` — Anthropic Claude
- `gemini_client.py` — Google Gemini
- `deepseek_client.py` — DeepSeek（OpenAIClient のサブクラス）

## Queue 制御

- Ollama: `asyncio.Queue(maxsize=1)` で直列化（VRAM 競合防止）
- Cloud: `asyncio.Semaphore(cloud_concurrency)` で同時実行数制限

## 新プロバイダー追加手順

1. `BaseLLMClient` を継承したクラスを作る
2. `engine/llm/` に配置する
3. `router.py` の provider 登録に追加する
4. エンジン本体のコード変更は不要

## 禁止事項

- `LLMRouter` を経由せず provider client を直接呼ばない
- API key をコードに書かない（`.env` のみ）

## 関連 spec

- `docs/specs/runtime-architecture.md`（LLM provider の位置づけ）

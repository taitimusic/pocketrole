"""DeepSeek クラウド LLM クライアント実装。

OpenAI 互換 API を持つため OpenAIClient の薄いサブクラスとして実装する。
base_url と provider_name のみ差し替える。
"""

from __future__ import annotations

from engine.llm.openai_client import OpenAIClient


class DeepSeekClient(OpenAIClient):
    """OpenAI 互換 API を持つ DeepSeek クライアント。"""

    BASE_URL = "https://api.deepseek.com/v1"

    def __init__(
        self,
        api_key: str,
        default_model: str = "deepseek-chat",
        timeout_sec: int = 60,
        max_retries: int = 3,
    ) -> None:
        super().__init__(
            api_key=api_key,
            default_model=default_model,
            timeout_sec=timeout_sec,
            max_retries=max_retries,
            base_url=self.BASE_URL,
        )

    @property
    def provider_name(self) -> str:
        return "deepseek"

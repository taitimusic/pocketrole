"""LLMクライアントの共通インターフェース定義。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class LLMResponse:
    """LLMからの応答を格納するデータクラス。"""

    text: str                        # 生成テキスト
    model: str                       # 使用モデル名
    provider: str                    # プロバイダー名（小文字）
    prompt_tokens: int | None        # 入力トークン数（取得不可の場合 None）
    completion_tokens: int | None    # 出力トークン数（取得不可の場合 None）
    latency_ms: int                  # 応答時間（ミリ秒）
    reasoning_present: bool = False  # thinking / reasoning が含まれていたか
    reasoning_chars: int = 0         # thinking / reasoning の文字数
    done_reason: str | None = None   # プロバイダー由来の終了理由
    completion_status: str = "complete"  # complete / empty_final / error 等

    @property
    def final_text(self) -> str:
        """永続化や本文表示に使う最終テキスト。"""
        return self.text


class BaseLLMClient(ABC):
    """全LLMプロバイダーの抽象基底クラス。

    新プロバイダーを追加する際はこのクラスを継承し、
    全抽象メソッド・プロパティを実装すること。
    """

    @abstractmethod
    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float = 0.8,
        max_tokens: int = 300,
        reasoning_mode: str = "auto",
        request_tag: str | None = None,
    ) -> LLMResponse:
        """テキストを生成する。

        Args:
            system_prompt: システムプロンプト
            user_prompt: ユーザープロンプト
            model: 使用モデル名
            temperature: 生成温度（0.0〜1.0）
            max_tokens: 最大出力トークン数
            reasoning_mode: reasoning / thinking の扱い（auto / on / off）

        Returns:
            LLMResponse

        Raises:
            LLMConnectionError: 接続失敗
            LLMTimeoutError: タイムアウト
            LLMRateLimitError: レート制限
            LLMAuthError: 認証エラー
            LLMModelNotFoundError: モデル未存在
            LLMResponseError: 応答フォーマット不正
            LLMQuotaExceededError: クォータ超過
        """

    @abstractmethod
    async def health_check(self) -> bool:
        """プロバイダーへの疎通確認。

        Returns:
            True: 正常、False: 異常
        """

    @abstractmethod
    async def list_models(self) -> list[str]:
        """利用可能なモデル名の一覧を返す。

        Returns:
            モデル名のリスト
        """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """プロバイダー識別子（小文字）。例: "ollama", "openai"。"""

    @property
    @abstractmethod
    def supports_concurrent(self) -> bool:
        """並行リクエストをサポートするか。

        True: asyncio.Semaphore で並行制限
        False: asyncio.Queue で直列処理（Ollama等）
        """

"""LLM操作で発生しうる例外クラス群。"""

from __future__ import annotations


class LLMError(Exception):
    """LLM関連エラーの基底クラス。"""


class LLMConnectionError(LLMError):
    """接続失敗（サーバー未起動・ネットワーク不可）。"""


class LLMTimeoutError(LLMError):
    """応答タイムアウト。"""


class LLMRateLimitError(LLMError):
    """レート制限超過（クラウドAPI用）。retry_after 属性を持つ。"""

    def __init__(self, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        msg = f"Rate limited. Retry after {retry_after}s" if retry_after else "Rate limited"
        super().__init__(msg)


class LLMAuthError(LLMError):
    """認証エラー（APIキー無効等）。"""


class LLMModelNotFoundError(LLMError):
    """指定モデルが存在しない。"""


class LLMResponseError(LLMError):
    """応答フォーマット不正（JSONパースエラー等）。"""


class LLMQuotaExceededError(LLMError):
    """クォータ超過・残高不足（クラウドAPI用）。"""

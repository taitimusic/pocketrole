"""
engine/news_selector.py — LLM フィルタによるニュース記事選択（時事モード用）

キャラのプロファイルと候補記事タイトルを LLM に投げ、最も興味を持ちそうな
1 件を選ばせる。失敗時はランダムフォールバック。
"""

from __future__ import annotations

import json
import logging
import random
from typing import Any

from db.news_store import NewsArticleStore
from engine.llm.router import LLMRouter
from engine.news_url import is_public_http_url
from engine.structured_generation import StructuredJSONPolicy, generate_structured_json

logger = logging.getLogger(__name__)

# 記事選択 structured generation のポリシー
_SELECTOR_POLICY = StructuredJSONPolicy(
    name="news_selector",
    response_max_tokens=64,
    repair_max_tokens=64,
    temperature=0.1,
    reasoning_mode="off",
    repair_reasoning_mode="off",
)


def _parse_index_response(text: str) -> int | None:
    """LLM の応答から {"index": N} を抽出する。"""
    text = text.strip()
    # JSON ブロックを探す
    start = text.find("{")
    end = text.rfind("}") + 1
    if start < 0 or end <= 0:
        return None
    try:
        data = json.loads(text[start:end])
        idx = data.get("index")
        if isinstance(idx, int):
            return idx
        if isinstance(idx, str) and idx.isdigit():
            return int(idx)
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return None


class NewsArticleSelector:
    """LLM フィルタでキャラに合う記事を選ぶ。

    使い方::

        selector = NewsArticleSelector(router, store, "ollama", "gemma4:e4b", 8)
        article = await selector.select_for_character(
            char=char_dict, story_id="ankoku_gakuen",
            exclude_urls=already_commented,
        )
    """

    def __init__(
        self,
        router: LLMRouter,
        store: NewsArticleStore,
        llm_provider: str,
        llm_model: str,
        candidates_for_llm: int = 8,
    ) -> None:
        self._router = router
        self._store = store
        self._llm_provider = llm_provider
        self._llm_model = llm_model
        self._candidates_for_llm = candidates_for_llm

    async def select_for_character(
        self,
        *,
        char: dict[str, Any],
        story_id: str,
        exclude_urls: set[str] | None = None,
        tag_filter: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """キャラに合う記事を 1 件選んで返す。記事がない場合は None。

        tag_filter: 空または None なら全記事プール（後方互換）。
                    非空ならタグ OR 絞り込みを行う。
        """
        if tag_filter:
            articles = await self._store.get_recent_articles_for_tag_names(tag_filter, limit=100)
        else:
            articles = await self._store.get_recent_articles(limit=100)
        articles = [
            a for a in articles
            if is_public_http_url(str(a.get("url") or ""))
        ]
        if exclude_urls:
            articles = [a for a in articles if a["url"] not in exclude_urls]
        if not articles:
            logger.debug("news_selector: no articles available story_id=%s", story_id)
            return None

        # 候補を最大 candidates_for_llm 件にランダム絞り込み
        candidates = random.sample(articles, min(self._candidates_for_llm, len(articles)))

        if len(candidates) == 1:
            return candidates[0]

        # LLM フィルタ
        char_name = str(char.get("name_ja") or char.get("name") or "")
        system_prompt = self._build_system_prompt(char, char_name)
        user_prompt = self._build_user_prompt(char_name, candidates)

        try:
            result = await generate_structured_json(
                router=self._router,
                provider=self._llm_provider,
                model=self._llm_model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                parser=_parse_index_response,
                repair_schema_prompt='{"index": 0}',
                policy=_SELECTOR_POLICY,
            )
            idx = result.parsed
            if isinstance(idx, int) and 0 <= idx < len(candidates):
                logger.debug(
                    "news_selector: llm selected index=%d char=%s", idx, char_name
                )
                return candidates[idx]
        except Exception as exc:
            logger.warning("news_selector: llm filter failed err=%s, fallback random", exc)

        # フォールバック: ランダム
        chosen = random.choice(candidates)
        logger.debug("news_selector: random fallback char=%s", char_name)
        return chosen

    @staticmethod
    def _build_system_prompt(char: dict[str, Any], char_name: str) -> str:
        personality = str(char.get("personality_core", ""))
        goal = str(char.get("current_goal", ""))
        worry = str(char.get("current_worry", ""))
        examples = char.get("speech_examples", [])
        examples_str = " / ".join(str(e) for e in (examples[:2] if examples else []))

        lines = [
            f"あなたは {char_name} の興味分野を判定するアシスタントです。",
            f"【性格】{personality}" if personality else "",
            f"【目標】{goal}" if goal else "",
            f"【悩み】{worry}" if worry else "",
            f"【口調例】{examples_str}" if examples_str else "",
        ]
        return "\n".join(l for l in lines if l)

    @staticmethod
    def _build_user_prompt(char_name: str, candidates: list[dict[str, Any]]) -> str:
        item_lines = []
        for i, a in enumerate(candidates):
            title = a.get("title", "")
            desc = (a.get("description") or "").strip()
            if desc:
                # LLM フィルタでは先頭 100 字で十分（選択判断に必要な趣旨を把握できる）
                item_lines.append(f"[{i}] {title}\n    概要: {desc[:100]}")
            else:
                item_lines.append(f"[{i}] {title}")
        items = "\n".join(item_lines)
        return (
            f"以下の {len(candidates)} 件のうち、{char_name} が最も興味を持ちそうな\n"
            f"1 件を選んでください。\n\n{items}\n\n"
            f'JSONのみで答えてください: {{"index": 番号}}'
        )

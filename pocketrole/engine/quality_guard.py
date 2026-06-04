"""engine/quality_guard.py — 発話品質の軽量ガード。"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any

from engine.config import QualityGuardConfig
from engine.story_style import StoryStyleProfile

_ABSTRACT_TOKENS = ("誰か", "本音", "飾り", "期待", "空っぽ")
_POETIC_TOKENS = ("暗闇", "運命", "空っぽ", "静か", "こぼれ", "揺れ", "本音")
_ABSTRACT_OPENING_TOKENS = ("気持ち", "想い", "何か", "どこか", "遠い", "揺れ", "心", "空気")
_CONCRETE_CUES = (
    "今", "ここ", "これ", "それ", "目", "手", "窓", "扉", "机", "椅子", "廊下", "屋上",
    "教室", "音楽室", "声", "音", "顔", "歌", "帰", "行", "見る", "聞", "触", "立", "座",
)
_HARD_REACTION_CUES = ("いや", "うん", "そう", "でも", "はい", "え", "違う", "わかった", "まだ", "なら", "じゃあ", "つまり", "本当に")
_SOFT_REACTION_CUES = ("ですか", "ますか", "なのか", "ってこと", "でしょう", "だろう", "なんです", "なの？", "です？")
_REACTION_STOP_WORDS = {"まだ", "帰る", "帰ら", "それ", "これ", "こと", "もの", "です", "ます"}
_GENERIC_REPLY_FOCUS_TEXTS = {"相手の主張への返答", "相手の確認への返答"}
_GENERIC_REPLY_TAILS = ("今の流れをここで返す", "今ここでちゃんと返す", "今の流れをそのまま流したくない")
_EMPTY_QUESTION_LOOP_TAILS = {
    "どうする",
    "どうするの",
    "どうするつもり",
    "どう思う",
    "どう見る",
    "聞かせて",
    "聞かせてください",
    "続きを返して",
    "続きを言って",
    "次を返して",
    "何か言いたいことは",
}
_FALLBACK_REPLY_TAILS = (
    "そこは今ここで決める",
    "そこはまだ曖昧にしない",
    "ここで決める",
    "今片づける",
)
_DIRECTOR_CUE_SURFACE_LEAKS = (
    "残った所を言葉にする",
    "残っている所を出して",
    "流れをどうする",
    "流れをどこで変える",
    "何から動くか今決める",
    "誰が動くか今決める",
    "どこまで言うか決める",
    "まだ終わりにしない",
    "そこは流さない",
    "そこは曖昧にしない",
    "ここで見る",
    "ここで確かめる",
    "目の前の一点",
)
_UNANCHORED_REPLY_FALLBACK_LOOP_LINES = {
    "その話は聞いた",
    "話を続けよう",
    "その流れは雑だ",
    "まだ終わってない",
    "まだ隠してる所を出してよ",
}
_HTTP_URL_RE = re.compile(r"https?://\S+", flags=re.IGNORECASE)


@dataclass
class QualityGuardResult:
    text: str
    action: str
    score: float
    issues: list[dict[str, Any]]


class QualityGuard:
    """発話テキストに軽量な品質ガードを適用する。"""

    def __init__(self, config: QualityGuardConfig) -> None:
        self._config = config

    def evaluate_hard(
        self,
        text: str,
        *,
        msg_type: str,
        sanitized_text: str | None = None,
        recent_dialogue_lines: list[str] | None = None,
    ) -> QualityGuardResult:
        """意味品質を見ない、保存前の構造ガードだけを適用する。"""
        if not self._config.enabled:
            return QualityGuardResult(text=sanitized_text if sanitized_text is not None else text, action="accept", score=1.0, issues=[])

        raw = text or ""
        cleaned = (sanitized_text if sanitized_text is not None else raw).strip()
        issues: list[dict[str, Any]] = []

        if not cleaned:
            issues.append(
                {
                    "issue_type": "empty_output",
                    "severity": "warning",
                    "details": {"msg_type": msg_type},
                }
            )

        if self._has_thinking_residue(raw):
            issues.append(
                {
                    "issue_type": "thinking_tag_output",
                    "severity": "warning",
                    "details": {"msg_type": msg_type},
                }
            )

        if self._has_markdown_or_code_output(raw):
            issues.append(
                {
                    "issue_type": "markdown_formatting",
                    "severity": "warning",
                    "details": {"msg_type": msg_type},
                }
            )

        if self._looks_like_json_or_code_output(raw, cleaned):
            issues.append(
                {
                    "issue_type": "json_or_code_output",
                    "severity": "warning",
                    "details": {"msg_type": msg_type},
                }
            )

        sentence_count = self._sentence_count(cleaned)
        if sentence_count > self._config.max_sentences:
            issues.append(
                {
                    "issue_type": "sentence_overflow",
                    "severity": "warning",
                    "details": {"msg_type": msg_type, "sentence_count": sentence_count, "limit": self._config.max_sentences},
                }
            )

        limit = self._limit_for_msg_type(msg_type)
        length_checked = self._text_for_length_check(cleaned, msg_type=msg_type)
        if len(length_checked) > limit:
            issues.append(
                {
                    "issue_type": "overlength",
                    "severity": "warning",
                    "details": {"msg_type": msg_type, "limit": limit, "length": len(length_checked)},
                }
            )

        director_cue = self._find_director_cue_surface_leak(cleaned)
        if director_cue is not None:
            issues.append(
                {
                    "issue_type": "director_cue_surface_leak",
                    "severity": "warning",
                    "details": {"phrase": director_cue},
                }
            )

        action = "retry" if issues else "accept"
        score = max(0.0, 1.0 - (0.3 * len(issues)))
        return QualityGuardResult(text=cleaned, action=action, score=score, issues=issues)

    def evaluate(
        self,
        text: str,
        *,
        msg_type: str,
        style_profile: StoryStyleProfile | None = None,
        target_char_name: str | None = None,
        recent_dialogue_lines: list[str] | None = None,
        scene_objective_text: str | None = None,
        dominant_signal_text: str | None = None,
        reply_focus_text: str | None = None,
        reply_focus_contract: dict[str, Any] | None = None,
        reply_signal_contract: dict[str, Any] | None = None,
        reply_surface_contract: dict[str, Any] | None = None,
        reply_variety_contract: dict[str, Any] | None = None,
        reply_dramatic_contract: dict[str, Any] | None = None,
        reply_blandness_contract: dict[str, Any] | None = None,
        reply_shape_contract: dict[str, Any] | None = None,
        reply_quality_contract: dict[str, Any] | None = None,
        reply_story_quality_contract: dict[str, Any] | None = None,
        reply_residual_quality_contract: dict[str, Any] | None = None,
        voice_anchor_text: str | None = None,
        quality_leniency: dict[str, float] | None = None,
    ) -> QualityGuardResult:
        if not self._config.enabled:
            return QualityGuardResult(text=text, action="accept", score=1.0, issues=[])

        action = "accept"
        issues: list[dict[str, Any]] = []
        soft_action: str | None = None
        paragraph_break_output = (
            msg_type != "current_affairs" and self._has_paragraph_break_output(text)
        )
        cleaned = self._strip_markdown(
            text,
            collapse_newlines=(msg_type != "current_affairs"),
        )
        if msg_type == "reply":
            normalized_reply = self._normalize_reply_surface(cleaned)
            if normalized_reply != cleaned:
                cleaned = normalized_reply
                soft_action = soft_action or "normalize"
                issues.append(
                    {
                        "issue_type": "reply_surface_normalized",
                        "severity": "info",
                        "details": {"msg_type": msg_type},
                    }
                )
        if paragraph_break_output:
            soft_action = "normalize"
            issues.append(
                {
                    "issue_type": "paragraph_break_output",
                    "severity": "warning",
                    "details": {"msg_type": msg_type},
                }
            )
        if cleaned != text:
            soft_action = soft_action or "normalize"
            issues.append(
                {
                    "issue_type": "markdown_formatting",
                    "severity": "warning",
                    "details": {"original_length": len(text)},
                }
            )

        limit = self._limit_for_msg_type(msg_type)
        sentence_count = self._sentence_count(cleaned)
        if sentence_count > self._config.max_sentences:
            cleaned = self._truncate_to_sentence_limit(cleaned, self._config.max_sentences)
            soft_action = "shorten"
            issues.append(
                {
                    "issue_type": "sentence_overflow",
                    "severity": "warning",
                    "details": {"msg_type": msg_type, "limit": self._config.max_sentences},
                }
            )

        length_checked = self._text_for_length_check(cleaned, msg_type=msg_type)
        if len(length_checked) > limit:
            if msg_type == "current_affairs":
                cleaned = self._truncate_current_affairs_preserving_urls(cleaned, limit)
            else:
                cleaned = self._truncate_to_char_limit(cleaned, limit)
            soft_action = "shorten"
            issues.append(
                {
                    "issue_type": "overlength",
                    "severity": "warning",
                    "details": {"limit": limit},
                }
            )
        if soft_action is not None:
            action = soft_action

        repeated_token = self._find_abstract_repetition(cleaned)
        if repeated_token is not None:
            action = "drop"
            issues.append(
                {
                    "issue_type": "abstract_repetition",
                    "severity": "warning",
                    "details": {"token": repeated_token},
                }
            )

        poetic_token = self._find_poetic_abstraction(cleaned, style_profile)
        if poetic_token is not None and action != "drop":
            action = "retry"
            issues.append(
                {
                    "issue_type": "poetic_abstraction",
                    "severity": "warning",
                    "details": {"token": poetic_token},
                }
            )

        if action != "drop" and self._looks_like_multi_clause_heaviness(cleaned):
            if action != "retry":
                action = "retry"
            issues.append(
                {
                    "issue_type": "multi_clause_heaviness",
                    "severity": "warning",
                    "details": {"msg_type": msg_type},
                }
            )

        if (
            msg_type == "reply"
            and action not in {"drop", "retry"}
            and self._looks_like_abstract_opening(
                cleaned,
                target_char_name=target_char_name,
            )
        ):
            action = "retry"
            issues.append(
                {
                    "issue_type": "abstract_opening",
                    "severity": "warning",
                    "details": {"target_char_name": target_char_name},
                }
            )

        if (
            dominant_signal_text
            and action != "drop"
            and self._looks_like_signal_override(cleaned, dominant_signal_text)
        ):
            if action != "retry":
                action = "retry"
            issues.append(
                {
                    "issue_type": "signal_override",
                    "severity": "warning",
                    "details": {"dominant_signal_text": dominant_signal_text},
                }
            )

        if action not in {"drop", "retry"} and self._is_low_concreteness(cleaned):
            action = "retry"
            issues.append(
                {
                    "issue_type": "low_concreteness",
                    "severity": "warning",
                    "details": {"msg_type": msg_type},
                }
            )

        if (
            msg_type == "monologue"
            and action not in {"drop", "retry"}
            and recent_dialogue_lines
        ):
            action = "retry"
            issues.append(
                {
                    "issue_type": "monologue_in_conversation_context",
                    "severity": "warning",
                    "details": {"target_char_name": target_char_name},
                }
            )

        if msg_type == "reply" and action not in {"drop", "retry"}:
            reply_reaction = self._classify_reply_direct_reaction(
                cleaned,
                target_char_name=target_char_name,
                recent_dialogue_lines=recent_dialogue_lines or [],
                reply_focus_text=reply_focus_text,
                reply_focus_contract=reply_focus_contract,
            )
            if reply_reaction == "normalize":
                if action == "accept":
                    action = "normalize"
                issues.append(
                    {
                        "issue_type": "reply_direct_reaction_soft",
                        "severity": "info",
                        "details": {"target_char_name": target_char_name},
                    }
                )
            elif reply_reaction == "retry":
                action = "retry"
                issues.append(
                    {
                        "issue_type": "reply_without_direct_reaction",
                        "severity": "warning",
                        "details": {
                            "target_char_name": target_char_name,
                            **(
                                self._reply_direct_reaction_diagnostics(
                                    cleaned,
                                    reply_focus_contract=reply_focus_contract,
                                )
                                if reply_focus_contract is not None
                                else {}
                            ),
                        },
                    }
                )

        if (
            msg_type == "reply"
            and reply_focus_text
            and action != "drop"
            and self._looks_like_reply_focus_missing(
                cleaned,
                reply_focus_text,
                reply_focus_contract=reply_focus_contract,
            )
        ):
            action = "retry"
            focus_missing_detected = True
            issues.append(
                {
                    "issue_type": "reply_focus_missing",
                    "severity": "warning",
                    "details": {
                        "reply_focus_text": reply_focus_text,
                        **(
                            self._reply_focus_contract_diagnostics(cleaned, reply_focus_contract)
                            if reply_focus_contract is not None
                            else {}
                        ),
                    },
                }
            )
        else:
            focus_missing_detected = False

        if (
            msg_type == "reply"
            and action != "drop"
            and not focus_missing_detected
            and reply_signal_contract is not None
        ):
            visibility_issues = self._analyze_reply_signal_visibility(
                cleaned,
                reply_signal_contract=reply_signal_contract,
                reply_focus_contract=reply_focus_contract,
            )
            for visibility_issue in visibility_issues:
                if str(visibility_issue.get("severity") or "") == "warning":
                    action = "retry"
                elif action == "accept":
                    action = "normalize"
                issues.append(visibility_issue)

        if msg_type in {"reply", "group"} and action != "drop":
            unanchored_fallback_line = (
                self._find_unanchored_reply_fallback_loop_line(cleaned)
                if msg_type == "reply"
                else None
            )
            generic_tail_blocking = (
                self._looks_like_empty_question_loop_tail(cleaned)
                or unanchored_fallback_line is not None
            )
            generic_tail_seen = generic_tail_blocking or (
                msg_type == "reply" and self._looks_like_generic_reply_tail(cleaned)
            )
        else:
            unanchored_fallback_line = None
            generic_tail_blocking = False
            generic_tail_seen = False

        if generic_tail_seen:
            if generic_tail_blocking:
                action = "retry"
            elif action == "accept":
                action = "normalize"
            issues.append(
                {
                    "issue_type": "generic_reply_tail",
                    "severity": "warning",
                    "details": {
                        "msg_type": msg_type,
                        "flat_issue_family": "generic_tail",
                        "generic_reply_tail_blocking": generic_tail_blocking,
                        **(
                            {"fallback_loop_line": unanchored_fallback_line}
                            if unanchored_fallback_line is not None
                            else {}
                        ),
                        **(
                            {"reply_surface_mode": str(reply_surface_contract.get("surface_mode") or "").strip()}
                            if reply_surface_contract
                            else {}
                        ),
                    },
                }
            )

        director_cue = self._find_director_cue_surface_leak(cleaned)
        if director_cue is not None and action != "drop":
            action = "retry"
            issues.append(
                {
                    "issue_type": "director_cue_surface_leak",
                    "severity": "warning",
                    "details": {"phrase": director_cue},
                }
            )

        voice_flat_details = self._analyze_reply_surface_flatness(
            cleaned,
            voice_anchor_text=voice_anchor_text,
            reply_surface_contract=reply_surface_contract,
            reply_variety_contract=reply_variety_contract,
            reply_dramatic_contract=reply_dramatic_contract,
            reply_blandness_contract=reply_blandness_contract,
            reply_shape_contract=reply_shape_contract,
            reply_quality_contract=reply_quality_contract,
            reply_story_quality_contract=reply_story_quality_contract,
            reply_residual_quality_contract=reply_residual_quality_contract,
        )
        if (
            msg_type == "reply"
            and action != "drop"
            and not focus_missing_detected
            and voice_flat_details is not None
        ):
            focus_diag = (
                self._reply_focus_contract_diagnostics(cleaned, reply_focus_contract)
                if reply_focus_contract is not None
                else {}
            )
            focus_family = str((reply_focus_contract or {}).get("focus_family") or "").strip()
            if focus_family and "reply_focus_family" not in voice_flat_details:
                voice_flat_details["reply_focus_family"] = focus_family
            voice_flat_blocking = self._is_blocking_voice_flat_details(voice_flat_details)
            if (
                not voice_flat_blocking
                and self._should_skip_residual_voice_flat_issue(
                    voice_flat_details,
                    focus_diagnostics=focus_diag,
                    text=cleaned,
                )
            ):
                voice_flat_details = None
            if voice_flat_details is None:
                voice_flat_blocking = False
            if voice_flat_details is not None and voice_flat_blocking:
                action = "retry"
                severity = "warning"
            elif voice_flat_details is not None:
                if action == "accept":
                    action = "normalize"
                severity = "info"
            else:
                severity = "info"
            if voice_flat_details is not None:
                issues.append(
                    {
                        "issue_type": "voice_flat_reply",
                        "severity": severity,
                        "details": {
                            "msg_type": msg_type,
                            "voice_anchor_text": voice_anchor_text,
                            "reply_focus_family": (
                                str((reply_focus_contract or {}).get("focus_family") or "").strip() or None
                            ),
                            "voice_flat_blocking": voice_flat_blocking,
                            "voice_flat_residual_only": not voice_flat_blocking,
                            **voice_flat_details,
                        },
                    }
                )

        if (
            scene_objective_text
            and scene_objective_text != "自然発生の会話"
            and action != "drop"
            and self._looks_like_scene_objective_drift(cleaned, scene_objective_text)
        ):
            if action != "retry":
                action = "retry"
            issues.append(
                {
                    "issue_type": "scene_objective_drift",
                    "severity": "warning",
                    "details": {"scene_objective_text": scene_objective_text},
                }
            )

        action = self._apply_quality_leniency(
            action,
            issues,
            quality_leniency=quality_leniency,
            soft_action=soft_action,
        )
        score = max(0.0, 1.0 - (0.3 * len(issues)))
        return QualityGuardResult(text=cleaned, action=action, score=score, issues=issues)

    def _apply_quality_leniency(
        self,
        action: str,
        issues: list[dict[str, Any]],
        *,
        quality_leniency: dict[str, float] | None,
        soft_action: str | None,
    ) -> str:
        if not quality_leniency or action not in {"retry", "drop"}:
            return action

        hard_issue_types = [
            str(issue.get("issue_type") or "").strip()
            for issue in issues
            if str(issue.get("severity") or "").strip() == "warning"
        ]
        if not hard_issue_types:
            return action
        if "reply_focus_missing" in hard_issue_types:
            return action

        if action == "retry":
            if all(float(quality_leniency.get(issue_type, 0.0)) >= 0.75 for issue_type in hard_issue_types):
                return soft_action or "accept"
            return action

        if all(float(quality_leniency.get(issue_type, 0.0)) >= 0.95 for issue_type in hard_issue_types):
            return "retry"
        return action

    def _limit_for_msg_type(self, msg_type: str) -> int:
        if msg_type == "group":
            return self._config.max_group_chars
        if msg_type == "reply":
            return self._config.max_reply_chars
        if msg_type == "current_affairs":
            return self._config.max_current_affairs_chars
        return self._config.max_monologue_chars

    @staticmethod
    def _text_for_length_check(text: str, *, msg_type: str) -> str:
        if msg_type != "current_affairs":
            return text
        return _HTTP_URL_RE.sub("", text).strip()

    def _truncate_current_affairs_preserving_urls(self, text: str, limit: int) -> str:
        urls = _HTTP_URL_RE.findall(text)
        body = _HTTP_URL_RE.sub("", text).strip()
        truncated_body = self._truncate_to_char_limit(body, limit)
        parts = [part for part in [truncated_body, *urls] if part]
        return "\n".join(parts).strip()

    @staticmethod
    def _has_thinking_residue(text: str) -> bool:
        return bool(
            re.search(r"<think(?:ing)?>|</think(?:ing)?>", text, flags=re.IGNORECASE)
            or re.search(r"(?m)^\s*(?:thinking|reasoning|analysis|internal)\s*[:：]", text, flags=re.IGNORECASE)
        )

    @staticmethod
    def _has_markdown_or_code_output(text: str) -> bool:
        if "```" in text:
            return True
        return bool(re.search(r"(?m)^\s{0,3}(?:#{1,6}|[>*+-])\s+", text))

    @staticmethod
    def _looks_like_json_or_code_output(raw_text: str, cleaned_text: str) -> bool:
        candidates = [raw_text.strip(), cleaned_text.strip()]
        for candidate in candidates:
            if not candidate:
                continue
            if candidate.startswith(("```json", "```")):
                return True
            if candidate[0] in "{[" and candidate[-1:] in "}]":
                try:
                    json.loads(candidate)
                    return True
                except json.JSONDecodeError:
                    if re.search(r'["\'](?:message|text|dialogue|utterance|verdict)["\']\s*:', candidate):
                        return True
            if re.search(r"(?m)^\s*(?:def|class|function|const|let|var)\s+", candidate):
                return True
        return False

    def _strip_markdown(self, text: str, *, collapse_newlines: bool = True) -> str:
        # thinking タグを最初に除去（Qwen3 等 thinking 系モデルの漏れ対策）
        cleaned = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", text, flags=re.DOTALL | re.IGNORECASE)
        cleaned = cleaned.replace("**", "").replace("__", "")
        cleaned = re.sub(r"(?m)^[#>\-\*\+]+\s*", "", cleaned)
        cleaned = cleaned.replace("```", "")
        if collapse_newlines:
            cleaned = re.sub(r"\s*\n+\s*", " ", cleaned)
        return cleaned.strip()

    def _truncate_to_sentence_limit(self, text: str, limit: int) -> str:
        parts = [part.strip() for part in re.split(r"([。！？!?])", text.strip()) if part.strip()]
        sentences: list[str] = []
        current = ""
        for part in parts:
            current += part
            if part in "。！？!?":
                sentences.append(current.strip())
                current = ""
        if current.strip():
            sentences.append(current.strip())
        if len(sentences) <= limit:
            return text.strip()
        return "".join(sentences[:limit]).strip()

    def _truncate_to_char_limit(self, text: str, limit: int) -> str:
        truncated = text[:limit].rstrip(" 、,.;:!?\n\t")
        if not truncated:
            return text[:limit].strip()
        return truncated

    def _has_paragraph_break_output(self, text: str) -> bool:
        return "\n" in text

    def _normalize_reply_surface(self, text: str) -> str:
        normalized = text.strip()
        normalized = re.sub(r"[ \t]+", " ", normalized)
        normalized = re.sub(r"^[「『（(]+", "", normalized)
        normalized = re.sub(r"[」』）)]+$", "", normalized)
        normalized = re.sub(r"([!?！？])。", r"\1", normalized)
        normalized = re.sub(r"([。！？!?]){2,}", lambda m: m.group(0)[0], normalized)
        return normalized.strip()

    def _sentence_count(self, text: str) -> int:
        sentences = [
            part.strip()
            for part in re.split(r"([。！？!?]+)\s*", text.strip())
            if part.strip()
        ]
        count = 0
        sentence_bodies: list[tuple[str, str]] = []
        current = ""
        punct = ""
        for part in sentences:
            if re.fullmatch(r"[。！？!?]+", part):
                punct = part
                if current.strip():
                    sentence_bodies.append((current.strip(), punct))
                    count += 1
                    current = ""
                    punct = ""
                continue
            current += part
        if current.strip():
            sentence_bodies.append((current.strip(), punct))
            count += 1

        if sentence_bodies:
            first_body, first_punct = sentence_bodies[0]
            compact_first = re.sub(r"^[「『（(]+|[」』）)]+$", "", first_body).strip()
            if (
                count > 1
                and len(compact_first) <= 8
                and first_punct
                and "。" not in first_punct
            ):
                count -= 1
        return count

    def _find_abstract_repetition(self, text: str) -> str | None:
        for token in _ABSTRACT_TOKENS:
            if text.count(token) > self._config.max_abstract_token_occurrences:
                return token
        return None

    def _find_poetic_abstraction(
        self,
        text: str,
        style_profile: StoryStyleProfile | None,
    ) -> str | None:
        matched = [token for token in _POETIC_TOKENS if token in text]
        threshold = 2
        if style_profile is not None and style_profile.quality_guard_mode == "light_banter":
            threshold = 2
        if len(matched) >= threshold:
            return matched[0]
        return None

    def _looks_like_multi_clause_heaviness(self, text: str) -> bool:
        matched = [token for token in _POETIC_TOKENS if token in text]
        return ("、" in text or "だけが" in text) and len(matched) >= 2

    def _looks_like_abstract_opening(
        self,
        text: str,
        *,
        target_char_name: str | None,
    ) -> bool:
        first_sentence = re.split(r"[。！？!?]\s*", text.strip(), maxsplit=1)[0]
        if target_char_name and target_char_name in first_sentence:
            return False
        if any(cue in first_sentence for cue in _CONCRETE_CUES):
            return False
        return any(token in first_sentence for token in _ABSTRACT_OPENING_TOKENS)

    def _is_low_concreteness(self, text: str) -> bool:
        if any(cue in text for cue in _CONCRETE_CUES):
            return False
        vague_tokens = ("何か", "どこか", "誰か", "気持ち", "想い", "遠い", "違う", "まだ")
        return sum(1 for token in vague_tokens if token in text) >= 2

    def _looks_like_scene_objective_drift(self, text: str, scene_objective_text: str) -> bool:
        objective = scene_objective_text
        expected_tokens: tuple[str, ...]
        if "張り合い" in objective:
            expected_tokens = ("張り", "勝", "負", "競", "譲", "自慢", "決め", "引", "流さ", "見せ場", "基準")
        elif "食い違い" in objective:
            expected_tokens = ("違", "誤解", "本当", "つまり", "どういう")
        elif "回収" in objective:
            expected_tokens = ("決め", "結局", "今", "なら", "終わ")
        elif "優劣" in objective:
            expected_tokens = ("勝", "負", "上", "下", "見せ", "自慢", "決め", "引", "基準")
        else:
            return False
        return not any(token in text for token in expected_tokens)

    def _looks_like_signal_override(self, text: str, dominant_signal_text: str) -> bool:
        dominant = dominant_signal_text
        expected_tokens: tuple[str, ...]
        if "張り合い" in dominant:
            expected_tokens = ("張り", "勝", "負", "競", "決め", "引", "見せ場", "基準")
        elif "本音" in dominant or "言ってない" in dominant:
            expected_tokens = ("本音", "言う", "まだ", "違")
        elif "見せ" in dominant or "自慢" in dominant:
            expected_tokens = ("見せ", "自慢", "勝", "負", "基準")
        elif "決め" in dominant or "回収" in dominant:
            expected_tokens = (
                "決め",
                "決め直",
                "終わ",
                "片づけ",
                "今",
                "回収",
                "流れ",
                "流さない",
                "曖昧にしない",
                "境界",
            )
        else:
            return False
        return not any(token in text for token in expected_tokens)

    def _find_director_cue_surface_leak(self, text: str) -> str | None:
        for phrase in _DIRECTOR_CUE_SURFACE_LEAKS:
            if phrase in text:
                return phrase
        if re.search(r"流れを.{0,4}どうする", text):
            return "流れをどうする"
        return None

    @staticmethod
    def _find_unanchored_reply_fallback_loop_line(text: str) -> str | None:
        cleaned = str(text or "").strip().rstrip("。！？!?")
        if not cleaned:
            return None
        tail = cleaned.split("、", maxsplit=1)[1].strip() if "、" in cleaned else cleaned
        tail = tail.rstrip("。！？!?")
        return tail if tail in _UNANCHORED_REPLY_FALLBACK_LOOP_LINES else None

    def _classify_reply_direct_reaction(
        self,
        text: str,
        *,
        target_char_name: str | None,
        recent_dialogue_lines: list[str],
        reply_focus_text: str | None = None,
        reply_focus_contract: dict[str, Any] | None = None,
    ) -> str:
        if not recent_dialogue_lines:
            return "accept"
        first_sentence = re.split(r"[。！？!?]\s*", text.strip(), maxsplit=1)[0]
        target_tokens = self._target_name_tokens(target_char_name)
        if any(token in first_sentence for token in target_tokens):
            return "accept"
        if any(cue in first_sentence for cue in _HARD_REACTION_CUES):
            return "accept"
        if reply_focus_contract and self._reply_direct_reaction_contract_satisfied(
            first_sentence,
            reply_focus_contract=reply_focus_contract,
        ):
            return "accept"
        if (
            reply_focus_contract
            and self._reply_focus_contract_satisfied(
                text,
                reply_focus_contract,
            )
        ):
            return "normalize"
        if self._shares_dialogue_token(first_sentence, recent_dialogue_lines):
            return "accept"
        if self._shares_reply_focus_token(first_sentence, reply_focus_text):
            return "accept"
        if "?" in first_sentence or "？" in first_sentence:
            return "normalize"
        if any(cue in first_sentence for cue in _SOFT_REACTION_CUES):
            return "normalize"
        return "retry"

    @staticmethod
    def _reply_direct_reaction_contract_satisfied(
        first_sentence: str,
        *,
        reply_focus_contract: dict[str, Any],
    ) -> bool:
        focus_window = str(first_sentence).strip()
        focus_family = str(reply_focus_contract.get("focus_family") or "").strip()
        focus_anchor_tokens = [
            str(token).strip()
            for token in list(reply_focus_contract.get("focus_anchor_tokens") or [])
            if str(token).strip()
        ]
        required_focus_cues = [
            str(token).strip()
            for token in list(reply_focus_contract.get("required_focus_cues") or [])
            if str(token).strip()
        ]
        if not focus_anchor_tokens and not required_focus_cues:
            return False
        if focus_anchor_tokens and any(token in focus_window for token in focus_anchor_tokens):
            return True
        preferred_action = str(reply_focus_contract.get("preferred_action") or "").strip()
        return QualityGuard._reply_focus_family_semantic_hit(
            focus_window,
            focus_family=focus_family,
            preferred_action=preferred_action,
        )

    def _reply_direct_reaction_diagnostics(
        self,
        text: str,
        *,
        reply_focus_contract: dict[str, Any],
    ) -> dict[str, Any]:
        first_sentence = re.split(r"[。！？!?]\s*", text.strip(), maxsplit=1)[0]
        anchor_seen = self._reply_direct_reaction_contract_satisfied(
            first_sentence,
            reply_focus_contract=reply_focus_contract,
        )
        return {
            "reply_focus_family": str(reply_focus_contract.get("focus_family") or "").strip() or None,
            "reply_direct_reaction_mode": "hard_retry",
            "reply_direct_anchor_seen": anchor_seen,
        }

    @staticmethod
    def _is_generic_reply_focus_text(reply_focus_text: str | None) -> bool:
        return str(reply_focus_text or "").strip() in _GENERIC_REPLY_FOCUS_TEXTS

    @staticmethod
    def _probe_response_tokens() -> tuple[str, ...]:
        return ("なぜ", "どこで", "どこに", "何を", "何で", "誰が", "いつ", "どこまで", "どうする")

    @staticmethod
    def _reply_focus_family_semantic_hit(
        focus_window: str,
        *,
        focus_family: str,
        preferred_action: str = "",
    ) -> bool:
        if focus_family == "timing":
            return any(token in focus_window for token in ("今", "先に", "あとで", "後で", "いつ", "時間", "タイミング")) or any(
                phrase in focus_window
                for phrase in (
                    "始まる前",
                    "その前",
                    "ここで固定",
                    "ここで切る",
                    "ここで区切る",
                    "先に固定",
                    "先に区切る",
                    "あとで決める",
                    "先に通す",
                    "動く前",
                    "今のうち",
                    "一回止める",
                    "いったん止める",
                    "いったん区切る",
                    "順番は先に決め",
                    "順番を先に決め",
                    "まだ濁す",
                    "タイミングだけ",
                )
            )
        if focus_family == "proposal" or preferred_action == "propose":
            return any(token in focus_window for token in ("なら", "じゃあ", "どう", "提案")) or any(
                phrase in focus_window
                for phrase in (
                    "代わりに",
                    "その前に",
                    "順番を変",
                    "順番を入れ替",
                    "先にコード",
                    "先にこれ",
                    "まずこっち",
                    "今のままでは終わらせない",
                    "押し切らせない",
                )
            )
        if focus_family == "decision_owner":
            return any(
                token in focus_window
                for token in (
                    "誰",
                    "私が",
                    "お前が",
                    "任せ",
                    "役",
                    "決める役",
                    "仕切",
                    "こっち",
                    "肩書",
                    "権利",
                    "主導権",
                )
            ) or (
                any(token in focus_window for token in ("俺が", "あたしが", "こっちが", "私が", "お前が"))
                and any(token in focus_window for token in ("主導権", "入れ替わり", "順番"))
            ) or any(
                phrase in focus_window
                for phrase in (
                    "入れ替わりは先に",
                    "入れ替わりを先に",
                    "入れ替わりをどうする",
                    "順番を通す",
                    "順番で通す",
                )
            )
        if focus_family == "basis":
            return any(
                token in focus_window for token in ("基準", "理由", "何で", "どこを", "何の", "どの", "判断", "定義")
            ) or any(
                phrase in focus_window
                for phrase in ("何で測る", "何で決める", "どの基準", "何の基準", "何を基準", "判断基準", "どう定義")
            )
        if focus_family == "premise":
            return any(token in focus_window for token in ("前提", "なら", "まず", "その前"))
        if focus_family == "yes_no":
            return any(token in focus_window for token in ("帰", "残", "行", "まだ"))
        return False

    def _shares_reply_focus_token(self, first_sentence: str, reply_focus_text: str | None) -> bool:
        if not reply_focus_text:
            return False
        if self._is_generic_reply_focus_text(reply_focus_text):
            return False
        if "帰るかどうか" in reply_focus_text:
            return any(token in first_sentence for token in ("帰", "残", "まだ", "行", "帰ら"))
        if "いつ動く" in reply_focus_text:
            return (
                any(token in first_sentence for token in ("いつ", "何時", "時間", "後で", "あとで", "今日", "明日", "タイミング"))
                or ("今" in first_sentence and "決" in first_sentence)
                or ("先" in first_sentence and "決" in first_sentence)
                or ("動" in first_sentence and "決" in first_sentence)
                or any(
                    phrase in first_sentence
                    for phrase in (
                        "始まる前",
                        "その前",
                        "ここで固定",
                        "先に固定",
                        "先に通す",
                        "順番は先に決め",
                        "順番を先に決め",
                        "まだ濁す",
                        "動く前",
                        "今のうち",
                        "タイミングだけ",
                    )
                )
            )
        if "誰が決める" in reply_focus_text:
            return any(
                token in first_sentence
                for token in ("誰", "決め", "順番", "先に", "どの", "コード", "役", "仕切", "こっち", "任せ", "肩書", "権利", "主導権")
            )
        if "判断基準" in reply_focus_text:
            return any(token in first_sentence for token in ("基準", "何の", "どの", "見せ場", "先に", "判断", "定義")) or any(
                phrase in first_sentence for phrase in ("判断基準", "どう定義")
            )
        if "前提" in reply_focus_text:
            return any(token in first_sentence for token in ("前提", "譲歩", "済む", "先に", "残り"))
        if "確認" in reply_focus_text:
            return any(token in first_sentence for token in ("確認", "本当", "つまり", "なの", "誰", "どの", "基準", "決め"))
        if "反論" in reply_focus_text or "異議" in reply_focus_text:
            return any(token in first_sentence for token in ("違", "でも", "いや", "違う"))
        if "提案" in reply_focus_text:
            return any(token in first_sentence for token in ("なら", "じゃあ", "先に", "どう", "提案")) or any(
                phrase in first_sentence
                for phrase in (
                    "代わりに",
                    "その前に",
                    "順番を変",
                    "順番を入れ替",
                    "今のままでは終わらせない",
                    "押し切らせない",
                )
            )
        return False

    def _looks_like_reply_focus_missing(
        self,
        text: str,
        reply_focus_text: str,
        *,
        reply_focus_contract: dict[str, Any] | None = None,
    ) -> bool:
        if self._is_generic_reply_focus_text(reply_focus_text):
            return False
        if reply_focus_contract is not None:
            return not self._reply_focus_contract_satisfied(text, reply_focus_contract)
        sentences = [part.strip() for part in re.split(r"[。！？!?]\s*", text.strip()) if part.strip()]
        focus_window = " ".join(sentences[:2]) if sentences else text.strip()
        if "論点" in reply_focus_text or ("食い違" in reply_focus_text and "論点" in reply_focus_text):
            return not any(
                token in focus_window
                for token in ("食い違", "論点", "どこ", "潰", "解消", "片づけ", "ほど", "主導権", "見せ場", "順番")
            )
        if "帰るかどうか" in reply_focus_text:
            return not any(token in focus_window for token in ("帰", "残", "まだ", "行", "帰ら"))
        if "いつ動く" in reply_focus_text:
            return not (
                any(token in focus_window for token in ("いつ", "何時", "時間", "後で", "あとで", "今日", "明日", "タイミング"))
                or ("今" in focus_window and "決" in focus_window)
                or ("先" in focus_window and "決" in focus_window)
                or ("動" in focus_window and "決" in focus_window)
                or any(
                    phrase in focus_window
                    for phrase in (
                        "始まる前",
                        "その前",
                        "ここで固定",
                        "先に固定",
                        "先に通す",
                        "動く前",
                        "今のうち",
                        "タイミングだけ",
                    )
                )
            )
        if "誰が決める" in reply_focus_text:
            return not any(
                token in focus_window
                for token in ("誰", "決め", "どの", "コード", "基準", "決める役", "役", "仕切", "こっち", "任せ", "肩書", "権利", "主導権")
            )
        if "判断基準" in reply_focus_text:
            return not any(token in focus_window for token in ("基準", "何の", "どの", "見せ場", "誰", "判断")) and not any(
                phrase in focus_window for phrase in ("判断基準",)
            )
        if "前提" in reply_focus_text:
            return not any(token in focus_window for token in ("前提", "譲歩", "済む", "本当", "誰", "残り"))
        if "確認" in reply_focus_text:
            return not any(
                token in focus_window
                for token in ("確認", "本当", "つまり", "なの", "ですか", "？", "?", "誰", "どの", "基準", "決め")
            )
        if "反論" in reply_focus_text or "異議" in reply_focus_text:
            return not any(token in focus_window for token in ("違", "でも", "いや", "違う"))
        if "提案" in reply_focus_text:
            return not (
                any(token in focus_window for token in ("なら", "じゃあ", "先に", "どう", "提案"))
                or any(
                    phrase in focus_window
                    for phrase in (
                        "代わりに",
                        "その前に",
                        "順番を変",
                        "順番を入れ替",
                        "今のままでは終わらせない",
                        "押し切らせない",
                    )
                )
            )
        # 汎用フォールバック: reply_focus_text から漢字複合語（2文字以上）を抽出して照合
        # 助詞・活用語尾（ひらがな）は除外し、意味を持つ漢字列のみを対象とする
        focus_tokens: set[str] = set(re.findall(r"[一-龠]{2,}", reply_focus_text))
        focus_tokens |= set(re.findall(r"[A-Za-z0-9]{2,}", reply_focus_text))
        focus_tokens -= _REACTION_STOP_WORDS
        if focus_tokens:
            return not any(token in focus_window for token in focus_tokens)
        return False

    @staticmethod
    def _reply_focus_contract_diagnostics(text: str, contract: dict[str, Any]) -> dict[str, Any]:
        sentences = [part.strip() for part in re.split(r"[。！？!?]\s*", text.strip()) if part.strip()]
        if not sentences:
            return {
                "reply_focus_family": str(contract.get("focus_family") or "").strip() or None,
                "reply_focus_anchor_seen": False,
                "reply_focus_semantic_missed": True,
            }
        must_answer_in_first = bool(contract.get("must_answer_in_first_sentence"))
        focus_window = sentences[0] if must_answer_in_first else " ".join(sentences[:2])
        required_tokens = [
            str(token).strip()
            for token in list(contract.get("required_tokens") or [])
            if str(token).strip()
        ]
        required_focus_cues = [
            str(token).strip()
            for token in list(contract.get("required_focus_cues") or [])
            if str(token).strip()
        ]
        focus_anchor_tokens = [
            str(token).strip()
            for token in list(contract.get("focus_anchor_tokens") or [])
            if str(token).strip()
        ]
        forbidden_focus_drift_tokens = {
            str(token).strip()
            for token in list(contract.get("forbidden_focus_drift_tokens") or [])
            if str(token).strip()
        }
        focus_family = str(contract.get("focus_family") or "").strip()
        preferred_action = str(contract.get("preferred_action") or "").strip()
        token_seen = any(token in focus_window for token in required_tokens) if required_tokens else False
        cue_seen = any(token in focus_window for token in required_focus_cues) if required_focus_cues else False
        anchor_seen = any(token in focus_window for token in focus_anchor_tokens) if focus_anchor_tokens else False
        family_cue_seen = QualityGuard._reply_focus_family_semantic_hit(
            focus_window,
            focus_family=focus_family,
            preferred_action=preferred_action,
        )
        followup_sentence = sentences[1] if must_answer_in_first and len(sentences) > 1 else ""
        followup_token_seen = any(token in followup_sentence for token in required_tokens) if required_tokens else False
        followup_cue_seen = any(token in followup_sentence for token in required_focus_cues) if required_focus_cues else False
        followup_anchor_seen = any(token in followup_sentence for token in focus_anchor_tokens) if focus_anchor_tokens else False
        followup_family_cue_seen = bool(followup_sentence) and (
            (
                focus_family == "decision_owner"
                and (
                    QualityGuard._reply_focus_family_semantic_hit(
                        followup_sentence,
                        focus_family=focus_family,
                        preferred_action=preferred_action,
                    )
                    or followup_anchor_seen
                    or followup_cue_seen
                    or followup_token_seen
                )
                and any(token in followup_sentence for token in ("誰が", "主導権", "役割", "担う", "決め直", "決める役"))
            )
            or (
                focus_family == "timing"
                and QualityGuard._reply_focus_family_semantic_hit(
                    followup_sentence,
                    focus_family=focus_family,
                    preferred_action=preferred_action,
                )
                and any(token in followup_sentence for token in ("順番", "切る", "区切る", "先に", "今ここで"))
            )
            or (
                focus_family == "proposal"
                and QualityGuard._reply_focus_family_semantic_hit(
                    followup_sentence,
                    focus_family=focus_family,
                    preferred_action=preferred_action,
                )
                and any(
                    token in followup_sentence
                    for token in ("じゃあ", "代わりに", "その前に", "順番", "固定", "変える", "入れ替")
                )
            )
            or (
                focus_family == "premise"
                and QualityGuard._reply_focus_family_semantic_hit(
                    followup_sentence,
                    focus_family=focus_family,
                    preferred_action=preferred_action,
                )
                and any(token in followup_sentence for token in ("前提", "その前", "流れ", "順番", "決め直"))
            )
        )
        drift_seen = any(token in focus_window for token in forbidden_focus_drift_tokens)
        semantic_missed = bool(
            required_focus_cues or focus_anchor_tokens or forbidden_focus_drift_tokens
        ) and (
            (
                required_focus_cues
                and not cue_seen
                and not token_seen
                and not family_cue_seen
                and not followup_family_cue_seen
            )
            or (
                focus_anchor_tokens
                and not anchor_seen
                and not family_cue_seen
                and not followup_family_cue_seen
            )
            or (
                drift_seen
                and not anchor_seen
                and not family_cue_seen
                and not followup_family_cue_seen
            )
        )
        return {
            "reply_focus_family": focus_family or None,
            "reply_focus_anchor_seen": anchor_seen,
            "reply_focus_family_cue_seen": family_cue_seen,
            "reply_focus_first_sentence_semantic_hit": family_cue_seen or token_seen or cue_seen or anchor_seen,
            "reply_focus_semantic_missed": semantic_missed,
        }

    @staticmethod
    def _reply_focus_contract_satisfied(text: str, contract: dict[str, Any]) -> bool:
        sentences = [part.strip() for part in re.split(r"[。！？!?]\s*", text.strip()) if part.strip()]
        if not sentences:
            return False
        must_answer_in_first = bool(contract.get("must_answer_in_first_sentence"))
        focus_window = sentences[0] if must_answer_in_first else " ".join(sentences[:2])
        required_tokens = [
            str(token).strip()
            for token in list(contract.get("required_tokens") or [])
            if str(token).strip()
        ]
        required_focus_cues = [
            str(token).strip()
            for token in list(contract.get("required_focus_cues") or [])
            if str(token).strip()
        ]
        focus_anchor_tokens = [
            str(token).strip()
            for token in list(contract.get("focus_anchor_tokens") or [])
            if str(token).strip()
        ]
        forbidden_focus_drift_tokens = {
            str(token).strip()
            for token in list(contract.get("forbidden_focus_drift_tokens") or [])
            if str(token).strip()
        }
        focus_family = str(contract.get("focus_family") or "").strip()
        preferred_action = str(contract.get("preferred_action") or "").strip()
        token_seen = any(token in focus_window for token in required_tokens) if required_tokens else False
        cue_seen = any(token in focus_window for token in required_focus_cues) if required_focus_cues else False
        anchor_seen = any(token in focus_window for token in focus_anchor_tokens) if focus_anchor_tokens else False
        family_cue_seen = QualityGuard._reply_focus_family_semantic_hit(
            focus_window,
            focus_family=focus_family,
            preferred_action=preferred_action,
        )
        followup_sentence = sentences[1] if must_answer_in_first and len(sentences) > 1 else ""
        followup_token_seen = any(token in followup_sentence for token in required_tokens) if required_tokens else False
        followup_cue_seen = any(token in followup_sentence for token in required_focus_cues) if required_focus_cues else False
        followup_anchor_seen = any(token in followup_sentence for token in focus_anchor_tokens) if focus_anchor_tokens else False
        followup_family_cue_seen = bool(followup_sentence) and (
            (
                focus_family == "decision_owner"
                and (
                    QualityGuard._reply_focus_family_semantic_hit(
                        followup_sentence,
                        focus_family=focus_family,
                        preferred_action=preferred_action,
                    )
                    or followup_anchor_seen
                    or followup_cue_seen
                    or followup_token_seen
                )
                and any(token in followup_sentence for token in ("誰が", "主導権", "役割", "担う", "決め直", "決める役"))
            )
            or (
                focus_family == "timing"
                and QualityGuard._reply_focus_family_semantic_hit(
                    followup_sentence,
                    focus_family=focus_family,
                    preferred_action=preferred_action,
                )
                and any(token in followup_sentence for token in ("順番", "切る", "区切る", "先に", "今ここで"))
            )
            or (
                focus_family == "proposal"
                and QualityGuard._reply_focus_family_semantic_hit(
                    followup_sentence,
                    focus_family=focus_family,
                    preferred_action=preferred_action,
                )
                and any(
                    token in followup_sentence
                    for token in ("じゃあ", "代わりに", "その前に", "順番", "固定", "変える", "入れ替")
                )
            )
            or (
                focus_family == "premise"
                and QualityGuard._reply_focus_family_semantic_hit(
                    followup_sentence,
                    focus_family=focus_family,
                    preferred_action=preferred_action,
                )
                and any(token in followup_sentence for token in ("前提", "その前", "流れ", "順番", "決め直"))
            )
        )
        drift_seen = any(token in focus_window for token in forbidden_focus_drift_tokens)
        if (
            required_tokens
            and not token_seen
            and not family_cue_seen
            and not followup_family_cue_seen
            and not (cue_seen and (anchor_seen or not focus_anchor_tokens))
        ):
            return False
        if focus_anchor_tokens and not anchor_seen and not family_cue_seen and not followup_family_cue_seen:
            return False
        if required_focus_cues and not cue_seen and not token_seen and not family_cue_seen and not followup_family_cue_seen:
            return False
        if drift_seen and not anchor_seen and not family_cue_seen and not followup_family_cue_seen:
            return False
        preferred_action = str(contract.get("preferred_action") or "").strip()
        if preferred_action == "confirm":
            return any(
                token in focus_window
                for token in (
                    "決め",
                    "確認",
                    "出す",
                    "合わせ",
                    "帰",
                    "残",
                    "まだ",
                    "行",
                    "いつ",
                    "誰",
                    "基準",
                    "前提",
                )
            ) or cue_seen or anchor_seen or family_cue_seen
        if preferred_action == "reject":
            return any(token in focus_window for token in ("違", "いや", "でも", "無理"))
        if preferred_action == "propose":
            return (
                any(token in focus_window for token in ("なら", "じゃあ", "どう", "先に", "提案"))
                or family_cue_seen
                or followup_family_cue_seen
            )
        if preferred_action == "defer":
            return any(token in focus_window for token in ("あとで", "後で", "明日", "今は", "保留"))
        return True

    def _looks_like_generic_reply_tail(self, text: str) -> bool:
        sentences = [part.strip() for part in re.split(r"[。！？!?]\s*", text.strip()) if part.strip()]
        if len(sentences) < 2:
            return False
        second_sentence = sentences[1]
        if second_sentence in _FALLBACK_REPLY_TAILS:
            return False
        if any(token in second_sentence for token in _GENERIC_REPLY_TAILS):
            return True
        return second_sentence in {"確認します", "続けます", "返します"}

    def _looks_like_empty_question_loop_tail(self, text: str) -> bool:
        sentences = [part.strip() for part in re.split(r"[。！？!?]\s*", text.strip()) if part.strip()]
        if not sentences:
            return False
        tail = re.sub(r"^.+?、", "", sentences[-1]).strip()
        return tail in _EMPTY_QUESTION_LOOP_TAILS

    def _looks_like_voice_flat_reply(self, text: str, voice_anchor_text: str) -> bool:
        sentences = [part.strip() for part in re.split(r"[。！？!?]\s*", text.strip()) if part.strip()]
        if len(sentences) < 2:
            return False
        first_sentence_match = re.match(r"^(.+?[。！？!?])(?:\s|$)", text.strip())
        first_sentence = first_sentence_match.group(1) if first_sentence_match is not None else sentences[0]
        if "?" in first_sentence or "？" in first_sentence:
            return False
        second_sentence = sentences[1]
        if len(second_sentence) <= 1:
            return False
        if "熱血" in voice_anchor_text or "直球" in voice_anchor_text:
            return any(token in second_sentence for token in ("確認します", "整理します", "お願いします"))
        if "皮肉" in voice_anchor_text or "挑発" in voice_anchor_text:
            return not any(token in second_sentence for token in ("でしょ", "かよ", "もっと", "見せ", "黙る", "でしょう", "だろ", "じゃないか"))
        if "静かな確認" in voice_anchor_text or "丁寧" in voice_anchor_text:
            return any(token in second_sentence for token in ("勝負だ", "ぶつかる", "見せろ"))
        if "荒っぽい" in voice_anchor_text or "粗暴" in voice_anchor_text:
            return any(token in second_sentence for token in ("確認します", "よろしく", "ありがとう", "お願いします"))
        if "クール" in voice_anchor_text or "冷静" in voice_anchor_text:
            return any(token in second_sentence for token in ("嬉しい", "楽しい", "わくわく", "最高"))
        if "明るい" in voice_anchor_text or "元気" in voice_anchor_text:
            return any(token in second_sentence for token in ("無理", "どうせ", "しかたない", "疲れ"))
        return False

    def _analyze_reply_surface_flatness(
        self,
        text: str,
        *,
        voice_anchor_text: str | None,
        reply_surface_contract: dict[str, Any] | None,
        reply_variety_contract: dict[str, Any] | None,
        reply_dramatic_contract: dict[str, Any] | None,
        reply_blandness_contract: dict[str, Any] | None,
        reply_shape_contract: dict[str, Any] | None,
        reply_quality_contract: dict[str, Any] | None,
        reply_story_quality_contract: dict[str, Any] | None,
        reply_residual_quality_contract: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if (
            not voice_anchor_text
            and not reply_variety_contract
            and not reply_dramatic_contract
            and not reply_blandness_contract
            and not reply_shape_contract
            and not reply_quality_contract
            and not reply_story_quality_contract
            and not reply_residual_quality_contract
        ):
            return None
        sentences = [part.strip() for part in re.split(r"[。！？!?]\s*", text.strip()) if part.strip()]
        if len(sentences) < 2:
            return None
        first_sentence = sentences[0]
        second_sentence = sentences[1]
        preferred_voice_cues = [
            str(token).strip()
            for token in list((reply_surface_contract or {}).get("preferred_voice_cues") or [])
            if str(token).strip()
        ]
        recent_self_endings = [
            str(token).strip()
            for token in list((reply_surface_contract or {}).get("recent_self_endings") or [])
            if str(token).strip()
        ]
        forbidden_recent_endings = [
            str(token).strip()
            for token in list((reply_variety_contract or {}).get("forbidden_recent_endings") or [])
            if str(token).strip()
        ]
        forbidden_recent_second_beats = [
            str(token).strip()
            for token in list((reply_variety_contract or {}).get("forbidden_recent_second_beats") or [])
            if str(token).strip()
        ]
        forbidden_recent_openings = [
            str(token).strip()
            for token in list((reply_variety_contract or {}).get("forbidden_recent_openings") or [])
            if str(token).strip()
        ]
        recent_self_tail_reused = bool(
            reply_surface_contract
            and reply_surface_contract.get("must_vary_from_recent_self")
            and second_sentence in recent_self_endings
        )
        recent_opening_reused = first_sentence in forbidden_recent_openings
        recent_ending_reused = recent_self_tail_reused or second_sentence in forbidden_recent_endings
        has_preferred_voice_cue = any(token in second_sentence for token in preferred_voice_cues)
        preferred_move_tokens = [
            str(token).strip()
            for token in list((reply_variety_contract or {}).get("preferred_move_tokens") or [])
            if str(token).strip()
        ]
        second_beat_mode = str((reply_variety_contract or {}).get("second_beat_mode") or "").strip()
        actual_second_beat_mode = self._infer_reply_second_beat_mode(second_sentence)
        recent_second_beat_reused = bool(
            actual_second_beat_mode and actual_second_beat_mode in forbidden_recent_second_beats
        )
        second_beat_satisfied = self._reply_variety_second_beat_satisfied(
            second_sentence,
            second_beat_mode=second_beat_mode,
            preferred_move_tokens=preferred_move_tokens,
        )
        dramatic_move_mode = str((reply_dramatic_contract or {}).get("move_mode") or "").strip()
        dramatic_required_tokens = [
            str(token).strip()
            for token in list((reply_dramatic_contract or {}).get("required_move_tokens") or [])
            if str(token).strip()
        ]
        dramatic_semantic_family = str(
            (reply_dramatic_contract or {}).get("semantic_move_family") or ""
        ).strip()
        dramatic_pressure_anchor_tokens = [
            str(token).strip()
            for token in list((reply_dramatic_contract or {}).get("pressure_anchor_tokens") or [])
            if str(token).strip()
        ]
        dramatic_required_semantic_cues = [
            str(token).strip()
            for token in list((reply_dramatic_contract or {}).get("required_semantic_cues") or [])
            if str(token).strip()
        ]
        dramatic_forbidden_semantic_drifts = [
            str(token).strip()
            for token in list((reply_dramatic_contract or {}).get("forbidden_semantic_drifts") or [])
            if str(token).strip()
        ]
        forbidden_soft_landings = [
            str(token).strip()
            for token in list((reply_dramatic_contract or {}).get("forbidden_soft_landings") or [])
            if str(token).strip()
        ]
        forbidden_soft_landings.extend(
            str(token).strip()
            for token in list((reply_quality_contract or {}).get("forbidden_soft_landings") or [])
            if str(token).strip()
        )
        forbidden_soft_landings.extend(
            str(token).strip()
            for token in list((reply_story_quality_contract or {}).get("forbidden_soft_landings") or [])
            if str(token).strip()
        )
        forbidden_soft_landings.extend(
            str(token).strip()
            for token in list((reply_residual_quality_contract or {}).get("forbidden_soft_landings") or [])
            if str(token).strip()
        )
        forbidden_soft_landings = list(dict.fromkeys(token for token in forbidden_soft_landings if token))
        dramatic_move_seen = self._reply_dramatic_move_satisfied(
            second_sentence,
            move_mode=dramatic_move_mode,
            required_move_tokens=dramatic_required_tokens,
            semantic_move_family=dramatic_semantic_family,
            pressure_anchor_tokens=dramatic_pressure_anchor_tokens,
            required_semantic_cues=dramatic_required_semantic_cues,
            forbidden_semantic_drifts=dramatic_forbidden_semantic_drifts,
        )
        dramatic_anchor_seen = (
            not dramatic_pressure_anchor_tokens
            or any(token in second_sentence for token in dramatic_pressure_anchor_tokens)
        )
        dramatic_semantic_seen = self._reply_dramatic_semantic_family_satisfied(
            second_sentence,
            semantic_family=dramatic_semantic_family or dramatic_move_mode,
            required_semantic_cues=dramatic_required_semantic_cues,
        )
        dramatic_semantic_drift_used = any(
            token in second_sentence for token in dramatic_forbidden_semantic_drifts
        )
        dramatic_semantic_missed = bool(
            dramatic_semantic_family or dramatic_move_mode
        ) and (
            not dramatic_semantic_seen
            or not dramatic_anchor_seen
            or dramatic_semantic_drift_used
        )
        soft_landing_used = any(token in second_sentence for token in forbidden_soft_landings)
        shape_reused_recently = recent_opening_reused or recent_ending_reused
        generic_surface = bool(
            second_sentence in {"確認します", "続けます", "返します"}
            or any(token in second_sentence for token in ("整理します", "お願いします"))
            or second_sentence.endswith("します")
        )
        base_voice_flat = bool(voice_anchor_text) and self._looks_like_voice_flat_reply(text, voice_anchor_text or "")
        bland_second_sentence = (
            soft_landing_used
            or (dramatic_move_mode and not dramatic_move_seen)
            or (dramatic_move_mode == "counter" and ("出そう" in second_sentence or "しておこう" in second_sentence))
        )
        blandness_shape = str((reply_blandness_contract or {}).get("primary_shape") or "").strip()
        recent_shape_history = [
            str(token).strip()
            for token in list((reply_blandness_contract or {}).get("recent_shape_history") or [])
            if str(token).strip()
        ]
        pressure_shift_seen = (
            dramatic_move_seen
            or second_beat_satisfied
            or self._reply_pressure_shift_satisfied(
                second_sentence,
                shape=blandness_shape,
            )
        )
        blandness_shape_reused = bool(blandness_shape and blandness_shape in recent_shape_history)
        blandness_contract_missed = bool(
            reply_blandness_contract
            and (
                (
                    reply_blandness_contract.get("must_shift_pressure_in_second_sentence")
                    and blandness_shape_reused
                    and not pressure_shift_seen
                )
                or (
                    blandness_shape_reused
                    and any(
                        token in second_sentence
                        for token in list((reply_blandness_contract or {}).get("forbidden_soft_landings") or [])
                    )
                )
            )
        )
        shape_mode = str((reply_shape_contract or {}).get("primary_shape") or "").strip()
        shape_recent_history = [
            str(token).strip()
            for token in list((reply_shape_contract or {}).get("recent_shape_history") or [])
            if str(token).strip()
        ]
        story_pressure_tokens = [
            str(token).strip()
            for token in list((reply_shape_contract or {}).get("story_pressure_tokens") or [])
            if str(token).strip()
        ]
        shape_reused = bool(shape_mode and shape_mode in shape_recent_history)
        story_pressure_seen = any(token in second_sentence for token in story_pressure_tokens)
        story_flavor_weak = bool(story_pressure_tokens) and not story_pressure_seen
        shape_contract_missed = bool(
            reply_shape_contract
            and reply_shape_contract.get("must_shift_pressure_in_second_sentence")
            and shape_reused
            and not pressure_shift_seen
        )
        quality_shape = str((reply_quality_contract or {}).get("primary_shape") or "").strip()
        quality_second_beat = str((reply_quality_contract or {}).get("second_beat_mode") or "").strip()
        quality_story_pressure_tokens = [
            str(token).strip()
            for token in list((reply_quality_contract or {}).get("story_pressure_tokens") or [])
            if str(token).strip()
        ]
        quality_recent_shape_history = [
            str(token).strip()
            for token in list((reply_quality_contract or {}).get("recent_shape_history") or [])
            if str(token).strip()
        ]
        quality_recent_second_beat_history = [
            str(token).strip()
            for token in list((reply_quality_contract or {}).get("recent_second_beat_history") or [])
            if str(token).strip()
        ]
        reply_story_flavor_role = str(
            (reply_quality_contract or {}).get("required_story_flavor_role") or ""
        ).strip()
        reply_story_flavor_seen = any(token in second_sentence for token in quality_story_pressure_tokens)
        reply_second_beat_reused = bool(
            actual_second_beat_mode
            and quality_recent_second_beat_history.count(actual_second_beat_mode) >= 2
        )
        reply_quality_shape_reused = bool(
            quality_shape and quality_shape in quality_recent_shape_history
        )
        reply_quality_contract_missed = bool(
            reply_quality_contract
            and (
                (reply_quality_shape_reused and reply_second_beat_reused)
                or (soft_landing_used and not reply_story_flavor_seen)
            )
        )
        story_quality_shape = str(
            (reply_story_quality_contract or {}).get("primary_shape") or ""
        ).strip()
        story_quality_second_beat = str(
            (reply_story_quality_contract or {}).get("second_beat_mode") or ""
        ).strip()
        story_quality_story_pressure_tokens = [
            str(token).strip()
            for token in list((reply_story_quality_contract or {}).get("story_pressure_tokens") or [])
            if str(token).strip()
        ]
        story_quality_recent_shape_history = [
            str(token).strip()
            for token in list((reply_story_quality_contract or {}).get("recent_shape_history") or [])
            if str(token).strip()
        ]
        story_quality_recent_second_beat_history = [
            str(token).strip()
            for token in list((reply_story_quality_contract or {}).get("recent_second_beat_history") or [])
            if str(token).strip()
        ]
        reply_story_quality_role = str(
            (reply_story_quality_contract or {}).get("required_story_flavor_role") or ""
        ).strip()
        reply_story_quality_seen = any(
            token in second_sentence for token in story_quality_story_pressure_tokens
        )
        reply_story_quality_shape_reused = bool(
            story_quality_shape and story_quality_shape in story_quality_recent_shape_history
        )
        reply_story_quality_second_beat_reused = bool(
            actual_second_beat_mode
            and story_quality_recent_second_beat_history.count(actual_second_beat_mode) >= 2
        )
        reply_story_quality_contract_missed = bool(
            reply_story_quality_contract
            and (
                (
                    reply_story_quality_shape_reused
                    and reply_story_quality_second_beat_reused
                    and not pressure_shift_seen
                )
                or (soft_landing_used and not reply_story_quality_seen)
            )
        )
        residual_shape = str(
            (reply_residual_quality_contract or {}).get("primary_shape") or ""
        ).strip()
        residual_second_beat = str(
            (reply_residual_quality_contract or {}).get("second_beat_mode") or ""
        ).strip()
        residual_story_pressure_tokens = [
            str(token).strip()
            for token in list((reply_residual_quality_contract or {}).get("story_pressure_tokens") or [])
            if str(token).strip()
        ]
        residual_recent_shape_history = [
            str(token).strip()
            for token in list((reply_residual_quality_contract or {}).get("recent_shape_history") or [])
            if str(token).strip()
        ]
        residual_recent_second_beat_history = [
            str(token).strip()
            for token in list((reply_residual_quality_contract or {}).get("recent_second_beat_history") or [])
            if str(token).strip()
        ]
        residual_story_flavor_role = str(
            (reply_residual_quality_contract or {}).get("required_story_flavor_role") or ""
        ).strip()
        residual_story_flavor_seen = any(
            token in second_sentence for token in residual_story_pressure_tokens
        )
        residual_shape_reused = bool(
            residual_shape and residual_shape in residual_recent_shape_history
        )
        residual_second_beat_reused = bool(
            actual_second_beat_mode
            and residual_recent_second_beat_history.count(actual_second_beat_mode) >= 2
        )
        residual_block_threshold = str(
            (reply_residual_quality_contract or {}).get("residual_block_threshold") or ""
        ).strip()
        reply_residual_contract_missed = bool(
            reply_residual_quality_contract
            and (
                (
                    (
                        residual_block_threshold == "shape+second_beat+pressure_shift_or_story_flavor"
                        or not residual_block_threshold
                    )
                    and residual_shape_reused
                    and residual_second_beat_reused
                    and not pressure_shift_seen
                )
                or (soft_landing_used and not residual_story_flavor_seen)
            )
        )
        if not reply_story_flavor_role and reply_story_quality_role:
            reply_story_flavor_role = reply_story_quality_role
        if not reply_story_flavor_seen and reply_story_quality_seen:
            reply_story_flavor_seen = True
        if not reply_story_flavor_role and residual_story_flavor_role:
            reply_story_flavor_role = residual_story_flavor_role
        if not reply_story_flavor_seen and residual_story_flavor_seen:
            reply_story_flavor_seen = True
        if (
            not base_voice_flat
            and not recent_ending_reused
            and not recent_opening_reused
            and not recent_second_beat_reused
            and not bland_second_sentence
            and not shape_reused
            and not story_flavor_weak
            and not reply_second_beat_reused
            and not reply_quality_contract_missed
            and not reply_story_quality_contract_missed
            and not reply_residual_contract_missed
        ):
            return None
        surface_contract_missed = bool(
            recent_ending_reused and not has_preferred_voice_cue
        ) or bool(
            base_voice_flat and preferred_voice_cues and not has_preferred_voice_cue and generic_surface
        )
        variety_contract_missed = bool(
            (recent_opening_reused or recent_ending_reused)
            and second_beat_mode
            and not second_beat_satisfied
        ) or recent_second_beat_reused
        dramatic_contract_missed = bool(
            dramatic_move_mode
            and (
                dramatic_semantic_missed
                or ((not dramatic_move_seen or soft_landing_used) and shape_reused_recently)
            )
        )
        dramatic_contract_soft_missed = bool(
            dramatic_move_mode
            and not dramatic_contract_missed
            and (
                (dramatic_move_mode == "counter" and "出そう" in second_sentence)
                or (dramatic_move_mode and not dramatic_move_seen)
            )
        )
        if soft_landing_used:
            flat_issue_family = "soft_landing"
        elif shape_reused and not pressure_shift_seen:
            flat_issue_family = "shape_dominance"
        elif story_flavor_weak:
            flat_issue_family = "story_flavor_weak"
        elif reply_second_beat_reused:
            flat_issue_family = "reused_second_beat"
        elif recent_second_beat_reused:
            flat_issue_family = "reused_second_beat"
        elif blandness_shape_reused and not pressure_shift_seen:
            flat_issue_family = "shape_dominance"
        elif recent_ending_reused:
            flat_issue_family = "reused_ending"
        elif recent_opening_reused:
            flat_issue_family = "reused_opening"
        else:
            flat_issue_family = "voice_flat"
        return {
            "reply_surface_mode": str((reply_surface_contract or {}).get("surface_mode") or "").strip(),
            "reply_variety_shape": str((reply_variety_contract or {}).get("response_shape") or "").strip(),
            "reply_variety_second_beat": second_beat_mode,
            "flat_issue_family": flat_issue_family,
            "recent_self_tail_reused": recent_ending_reused,
            "recent_opening_reused": recent_opening_reused,
            "recent_ending_reused": recent_ending_reused,
            "recent_second_beat_reused": recent_second_beat_reused,
            "reply_dramatic_move_mode": dramatic_move_mode,
            "reply_dramatic_semantic_family": dramatic_semantic_family or dramatic_move_mode,
            "reply_dramatic_anchor_seen": dramatic_anchor_seen,
            "reply_dramatic_semantic_missed": dramatic_semantic_missed,
            "reply_dramatic_move_seen": dramatic_move_seen,
            "reply_soft_landing_used": soft_landing_used,
            "reply_shape_reused_recently": shape_reused_recently,
            "reply_shape_mode": shape_mode,
            "reply_shape_reused": shape_reused,
            "reply_blandness_shape": blandness_shape,
            "reply_blandness_shape_reused": blandness_shape_reused,
            "reply_pressure_shift_seen": pressure_shift_seen,
            "reply_story_pressure_seen": story_pressure_seen,
            "story_flavor_weak": story_flavor_weak,
            "reply_quality_shape": quality_shape,
            "reply_quality_second_beat": quality_second_beat,
            "reply_story_flavor_role": reply_story_flavor_role,
            "reply_story_flavor_seen": reply_story_flavor_seen,
            "reply_second_beat_reused": reply_second_beat_reused,
            "reply_story_quality_shape": story_quality_shape,
            "reply_story_quality_second_beat": story_quality_second_beat,
            "reply_story_quality_role": reply_story_quality_role,
            "reply_story_quality_seen": reply_story_quality_seen,
            "reply_story_quality_contract_missed": reply_story_quality_contract_missed,
            "reply_residual_shape": residual_shape,
            "reply_residual_second_beat": residual_second_beat,
            "reply_residual_story_flavor_role": residual_story_flavor_role,
            "reply_residual_story_flavor_seen": residual_story_flavor_seen,
            "reply_residual_second_beat_reused": residual_second_beat_reused,
            "reply_residual_contract_missed": reply_residual_contract_missed,
            "surface_contract_missed": surface_contract_missed,
            "variety_contract_missed": variety_contract_missed,
            "dramatic_contract_missed": dramatic_contract_missed,
            "dramatic_contract_soft_missed": dramatic_contract_soft_missed,
            "blandness_contract_missed": blandness_contract_missed,
            "shape_contract_missed": shape_contract_missed,
            "reply_quality_contract_missed": reply_quality_contract_missed,
        }

    @staticmethod
    def _is_blocking_voice_flat_details(details: dict[str, Any]) -> bool:
        blocking_flags = (
            "surface_contract_missed",
            "variety_contract_missed",
            "dramatic_contract_missed",
            "blandness_contract_missed",
            "shape_contract_missed",
            "reply_quality_contract_missed",
            "reply_story_quality_contract_missed",
            "reply_residual_contract_missed",
        )
        active_flags = {
            flag
            for flag in blocking_flags
            if bool(details.get(flag))
        }
        if not active_flags:
            return False
        if active_flags == {"dramatic_contract_missed"} and (
            bool(details.get("reply_dramatic_anchor_seen"))
            or bool(details.get("reply_dramatic_move_seen"))
            or bool(details.get("reply_story_pressure_seen"))
        ) and (
            bool(details.get("reply_pressure_shift_seen"))
            or bool(details.get("reply_story_pressure_seen"))
        ):
            return False
        if active_flags == {"dramatic_contract_missed", "shape_contract_missed"} and (
            bool(details.get("reply_dramatic_move_seen"))
            or bool(details.get("reply_story_pressure_seen"))
        ) and bool(details.get("reply_pressure_shift_seen")):
            return False
        if active_flags == {"dramatic_contract_missed"} and (
            str(details.get("flat_issue_family") or "") == "story_flavor_weak"
        ):
            if (
                str(details.get("reply_focus_family") or "") == "timing"
                and str(details.get("reply_shape_mode") or "") == "answer_then_probe"
                and bool(details.get("reply_pressure_shift_seen"))
                and not bool(details.get("reply_soft_landing_used"))
            ):
                return False
            if (
                str(details.get("reply_focus_family") or "") == "timing"
                and str(details.get("reply_shape_mode") or "") in {"answer_then_probe", "answer_then_press"}
                and not bool(details.get("reply_soft_landing_used"))
            ):
                return False
            if (
                str(details.get("reply_focus_family") or "") == "basis"
                and str(details.get("reply_shape_mode") or "") == "answer_then_condition"
                and bool(details.get("story_flavor_weak"))
                and not bool(details.get("reply_soft_landing_used"))
            ):
                return False
            if (
                str(details.get("reply_focus_family") or "") == "decision_owner"
                and str(details.get("reply_shape_mode") or "") == "answer_then_counter"
                and bool(details.get("reply_pressure_shift_seen"))
                and not bool(details.get("reply_soft_landing_used"))
            ):
                return False
            if (
                str(details.get("reply_focus_family") or "") == "decision_owner"
                and str(details.get("reply_shape_mode") or "") in {"answer_then_counter", "answer_then_press"}
                and not bool(details.get("reply_soft_landing_used"))
            ):
                return False
            if (
                str(details.get("reply_focus_family") or "") == ""
                and str(details.get("reply_shape_mode") or "") == "answer_then_redirect"
                and str(details.get("reply_dramatic_move_mode") or "") == "claim"
                and not bool(details.get("reply_soft_landing_used"))
            ):
                return False
            if (
                str(details.get("reply_focus_family") or "") in {"", "timing"}
                and str(details.get("reply_shape_mode") or "") == "answer_then_probe"
                and bool(details.get("reply_pressure_shift_seen"))
                and not bool(details.get("reply_soft_landing_used"))
            ):
                return False
        if active_flags == {"variety_contract_missed"} and str(details.get("flat_issue_family") or "") == "reused_second_beat":
            if bool(details.get("reply_pressure_shift_seen")) and (
                bool(details.get("reply_story_pressure_seen"))
                or bool(details.get("reply_dramatic_move_seen"))
            ):
                return False
        if active_flags == {"variety_contract_missed", "dramatic_contract_missed"} and (
            str(details.get("flat_issue_family") or "") == "reused_opening"
        ):
            if (
                str(details.get("reply_focus_family") or "") == "decision_owner"
                and str(details.get("reply_shape_mode") or "") == "answer_then_counter"
                and bool(details.get("reply_pressure_shift_seen"))
                and bool(details.get("reply_story_pressure_seen"))
                and bool(details.get("reply_dramatic_anchor_seen"))
                and not bool(details.get("reply_soft_landing_used"))
            ):
                return False
        if active_flags == {"variety_contract_missed", "dramatic_contract_missed"} and (
            str(details.get("flat_issue_family") or "") == "reused_second_beat"
        ):
            if (
                str(details.get("reply_focus_family") or "") == "timing"
                and str(details.get("reply_shape_mode") or "") == "answer_then_probe"
                and bool(details.get("reply_pressure_shift_seen"))
                and (
                    bool(details.get("reply_story_pressure_seen"))
                    or bool(details.get("reply_dramatic_move_seen"))
                )
                and not bool(details.get("reply_soft_landing_used"))
            ):
                return False
            if (
                str(details.get("reply_focus_family") or "") == "basis"
                and str(details.get("reply_shape_mode") or "") == "answer_then_condition"
                and bool(details.get("reply_pressure_shift_seen"))
                and bool(details.get("reply_story_pressure_seen"))
                and (
                    bool(details.get("reply_dramatic_anchor_seen"))
                    or bool(details.get("reply_second_beat_reused"))
                )
                and not bool(details.get("reply_soft_landing_used"))
            ):
                return False
        if active_flags == {"variety_contract_missed", "dramatic_contract_missed"} and (
            str(details.get("flat_issue_family") or "") == "story_flavor_weak"
        ):
            if (
                str(details.get("reply_focus_family") or "") == "timing"
                and str(details.get("reply_shape_mode") or "") == "answer_then_probe"
                and bool(details.get("reply_pressure_shift_seen"))
                and (
                    bool(details.get("reply_second_beat_reused"))
                    or bool(details.get("recent_second_beat_reused"))
                )
                and not bool(details.get("reply_soft_landing_used"))
            ):
                return False
            if (
                str(details.get("reply_focus_family") or "") == "decision_owner"
                and str(details.get("reply_shape_mode") or "") == "answer_then_counter"
                and bool(details.get("reply_pressure_shift_seen"))
                and not bool(details.get("reply_soft_landing_used"))
            ):
                return False
            if (
                str(details.get("reply_focus_family") or "") == "basis"
                and str(details.get("reply_shape_mode") or "") == "answer_then_condition"
                and bool(details.get("reply_pressure_shift_seen"))
                and bool(details.get("reply_story_pressure_seen"))
                and not bool(details.get("reply_soft_landing_used"))
            ):
                return False
        if active_flags == {"shape_contract_missed", "reply_story_quality_contract_missed"} and (
            str(details.get("flat_issue_family") or "") == "story_flavor_weak"
        ):
            if str(details.get("reply_focus_family") or "") in {"basis", "timing"} and (
                bool(details.get("reply_pressure_shift_seen"))
                or bool(details.get("reply_dramatic_move_seen"))
            ):
                return False
        if active_flags == {"shape_contract_missed"} and str(details.get("flat_issue_family") or "") == "shape_dominance":
            if (
                str(details.get("reply_focus_family") or "") == "basis"
                and str(details.get("reply_shape_mode") or "") == "answer_then_condition"
                and bool(details.get("story_flavor_weak"))
                and not bool(details.get("reply_soft_landing_used"))
            ):
                return False
        if active_flags == {"dramatic_contract_missed", "blandness_contract_missed", "shape_contract_missed"} and (
            str(details.get("flat_issue_family") or "") == "shape_dominance"
        ):
            if (
                str(details.get("reply_focus_family") or "") == "basis"
                and str(details.get("reply_shape_mode") or "") == "answer_then_condition"
                and bool(details.get("story_flavor_weak"))
                and not bool(details.get("reply_soft_landing_used"))
            ):
                return False
            if (
                str(details.get("reply_focus_family") or "") == "basis"
                and str(details.get("reply_shape_mode") or "") == "answer_then_condition"
                and bool(details.get("reply_dramatic_anchor_seen"))
                and bool(details.get("reply_story_pressure_seen"))
                and not bool(details.get("reply_soft_landing_used"))
            ):
                return False
        if (
            str(details.get("flat_issue_family") or "") == "voice_flat"
            and str(details.get("reply_focus_family") or "") == "basis"
            and str(details.get("reply_shape_mode") or "") == "answer_then_condition"
            and bool(details.get("reply_dramatic_anchor_seen"))
            and bool(details.get("reply_story_pressure_seen"))
            and not bool(details.get("reply_soft_landing_used"))
        ):
            return False
        if (
            str(details.get("flat_issue_family") or "") == "shape_dominance"
            and str(details.get("reply_focus_family") or "") == "basis"
            and str(details.get("reply_shape_mode") or "") == "answer_then_condition"
            and bool(details.get("reply_dramatic_anchor_seen"))
            and bool(details.get("reply_story_pressure_seen"))
            and not bool(details.get("reply_soft_landing_used"))
        ):
            active_contract_flags = {
                flag
                for flag in (
                    "variety_contract_missed",
                    "dramatic_contract_missed",
                    "blandness_contract_missed",
                    "shape_contract_missed",
                    "reply_quality_contract_missed",
                    "reply_story_quality_contract_missed",
                    "reply_residual_contract_missed",
                )
                if bool(details.get(flag))
            }
            if active_contract_flags and active_contract_flags.issubset(
                {
                    "variety_contract_missed",
                    "dramatic_contract_missed",
                    "blandness_contract_missed",
                    "shape_contract_missed",
                    "reply_quality_contract_missed",
                    "reply_story_quality_contract_missed",
                    "reply_residual_contract_missed",
                }
            ):
                return False
        return True

    @staticmethod
    def _should_skip_residual_voice_flat_issue(
        details: dict[str, Any],
        *,
        focus_diagnostics: dict[str, Any],
        text: str,
    ) -> bool:
        focus_family = str(
            details.get("reply_focus_family")
            or focus_diagnostics.get("reply_focus_family")
            or ""
        ).strip()
        flat_issue_family = str(details.get("flat_issue_family") or "").strip()
        shape_mode = str(details.get("reply_shape_mode") or "").strip()
        first_sentence_semantic_hit = bool(
            focus_diagnostics.get("reply_focus_first_sentence_semantic_hit")
        )
        first_sentence = re.split(r"[。！？!?]\s*", text.strip(), maxsplit=1)[0]
        split_sentences = [part.strip() for part in re.split(r"[。！？!?]\s*", text.strip()) if part.strip()]
        second_sentence = split_sentences[1] if len(split_sentences) > 1 else ""
        if (
            focus_family == "timing"
            and flat_issue_family == "story_flavor_weak"
            and shape_mode == "answer_then_probe"
            and first_sentence_semantic_hit
            and any(
                phrase in first_sentence
                for phrase in (
                    "順番は先に決め",
                    "順番を先に決め",
                    "まだ濁す",
                    "動く前",
                    "今のうち",
                    "ここで切る",
                    "ここで区切る",
                    "一回止める",
                    "いったん止める",
                    "いったん区切る",
                )
            )
            and bool(details.get("reply_pressure_shift_seen"))
            and not bool(details.get("reply_soft_landing_used"))
        ):
            return True
        if (
            focus_family == "timing"
            and flat_issue_family == "voice_flat"
            and shape_mode == "answer_then_probe"
            and first_sentence_semantic_hit
            and bool(details.get("reply_pressure_shift_seen"))
            and bool(details.get("reply_story_pressure_seen"))
            and not bool(details.get("reply_soft_landing_used"))
        ):
            return True
        if (
            focus_family == "decision_owner"
            and flat_issue_family in {"voice_flat", "story_flavor_weak"}
            and shape_mode in {"answer_then_counter", "answer_then_press"}
            and first_sentence_semantic_hit
            and (
                bool(details.get("reply_story_pressure_seen"))
                or any(
                    token in second_sentence
                    for token in ("主導権", "入れ替わり", "順番", "肩書", "曖昧にしない", "決め直")
                )
                or (
                    any(token in text for token in ("流れ", "回収"))
                    and any(token in text for token in ("主導権", "入れ替わり", "順番", "握る"))
                )
            )
            and not bool(details.get("reply_soft_landing_used"))
        ):
            return True
        if (
            focus_family == "timing"
            and flat_issue_family in {"voice_flat", "story_flavor_weak"}
            and shape_mode in {"answer_then_probe", "answer_then_press"}
            and first_sentence_semantic_hit
            and (
                any(
                    phrase in first_sentence
                    for phrase in (
                        "順番は先に決め",
                        "順番を先に決め",
                        "ここで区切る",
                        "ここで切る",
                        "どこで切る",
                        "今のうち",
                        "動く前",
                    )
                )
                or any(token in second_sentence for token in ("見せ場", "順番", "決め直", "区切", "主導権"))
                or (
                    "流れ" in second_sentence
                    and "どこで切る" in second_sentence
                    and ("先に" in second_sentence or "出して" in second_sentence)
                )
            )
            and not bool(details.get("reply_soft_landing_used"))
        ):
            return True
        if (
            focus_family == "premise"
            and flat_issue_family in {"voice_flat", "story_flavor_weak"}
            and shape_mode == "answer_then_condition"
            and first_sentence_semantic_hit
            and any(token in second_sentence for token in ("流れ", "順番", "決め直", "前提"))
            and not bool(details.get("reply_soft_landing_used"))
        ):
            return True
        if (
            not focus_family
            and flat_issue_family in {"voice_flat", "story_flavor_weak"}
            and shape_mode == "answer_then_redirect"
            and any(token in second_sentence for token in ("順番", "固定", "決める", "見せ場"))
            and not bool(details.get("reply_soft_landing_used"))
        ):
            return True
        if (
            not focus_family
            and flat_issue_family in {"voice_flat", "story_flavor_weak"}
            and shape_mode == "answer_then_probe"
            and any(token in first_sentence for token in ("主導権", "順番"))
            and any(token in second_sentence for token in ("ライン", "決めろ", "決める", "引く"))
            and not bool(details.get("reply_soft_landing_used"))
        ):
            return True
        if (
            focus_family == "basis"
            and flat_issue_family == "story_flavor_weak"
            and shape_mode == "answer_then_condition"
            and first_sentence_semantic_hit
            and bool(details.get("reply_pressure_shift_seen"))
            and any(
                token in second_sentence
                for token in ("見せ場", "置きどころ", "場所", "勝負", "主導権", "入れ替わり", "流れ", "回収")
            )
            and not bool(details.get("reply_soft_landing_used"))
        ):
            return True
        if (
            focus_family == "basis"
            and flat_issue_family == "voice_flat"
            and shape_mode == "answer_then_condition"
            and first_sentence_semantic_hit
            and bool(details.get("reply_pressure_shift_seen"))
            and bool(details.get("reply_story_pressure_seen"))
            and not bool(details.get("reply_soft_landing_used"))
        ):
            return True
        return False

    @staticmethod
    def _reply_pressure_shift_satisfied(second_sentence: str, *, shape: str) -> bool:
        if shape == "answer_then_counter":
            return any(token in second_sentence for token in ("先に", "出せ", "言え", "見せ", "違う"))
        if shape == "answer_then_condition":
            return any(
                token in second_sentence
                for token in (
                    "なら",
                    "そのあと",
                    "条件",
                    "先なら",
                    "固定",
                    "主導権",
                    "入れ替わり",
                    "見せ場",
                    "勝負",
                    "流れ",
                    "回収",
                    "順番",
                    "場所",
                )
            )
        if shape == "answer_then_probe":
            return any(token in second_sentence for token in (*QualityGuard._probe_response_tokens(), "？", "?"))
        return False

    @staticmethod
    def _analyze_reply_signal_visibility(
        text: str,
        *,
        reply_signal_contract: dict[str, Any],
        reply_focus_contract: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        sentences = [part.strip() for part in re.split(r"[。！？!?]\s*", text.strip()) if part.strip()]
        if not sentences:
            return []
        window_text = " ".join(sentences[:2])
        focus_diag = (
            QualityGuard._reply_focus_contract_diagnostics(text, reply_focus_contract)
            if reply_focus_contract is not None
            else {}
        )
        focus_family = str(focus_diag.get("reply_focus_family") or "").strip()
        first_sentence_semantic_hit = bool(focus_diag.get("reply_focus_first_sentence_semantic_hit"))
        first_sentence = sentences[0]
        visibility_mode = str(reply_signal_contract.get("visibility_mode") or "").strip()
        lenient_visibility = False
        if first_sentence_semantic_hit:
            if focus_family in {"decision_owner", "proposal"}:
                lenient_visibility = True
            elif focus_family == "timing":
                lenient_visibility = any(
                    phrase in first_sentence
                    for phrase in ("始まる前", "その前", "ここで固定", "先に固定", "あとで決める", "先に通す")
                )
                if not lenient_visibility and visibility_mode == "objective_first":
                    lenient_visibility = True
            elif focus_family == "premise":
                lenient_visibility = any(phrase in first_sentence for phrase in ("前提", "その前", "条件"))
        signal_tokens = [
            str(token).strip()
            for token in list(reply_signal_contract.get("signal_anchor_tokens") or [])
            if str(token).strip()
        ]
        objective_tokens = [
            str(token).strip()
            for token in list(reply_signal_contract.get("objective_anchor_tokens") or [])
            if str(token).strip()
        ]
        forbidden_generic_replacements = [
            str(token).strip()
            for token in list(reply_signal_contract.get("forbidden_generic_replacements") or [])
            if str(token).strip()
        ]
        basis_process_objective = (
            focus_family == "basis"
            and first_sentence_semantic_hit
            and any(token in {"流れ", "回収"} for token in objective_tokens)
        )
        basis_control_objective = (
            focus_family == "basis"
            and first_sentence_semantic_hit
            and any(token in {"主導権", "入れ替わり"} for token in objective_tokens)
        )
        basis_conflict_objective = (
            focus_family == "basis"
            and first_sentence_semantic_hit
            and any(token in {"食い違い", "対立"} for token in objective_tokens)
        )
        timing_control_objective = (
            focus_family == "timing"
            and first_sentence_semantic_hit
            and any(token in {"主導権", "入れ替わり"} for token in objective_tokens)
        )
        timing_dramatic_objective = (
            focus_family == "timing"
            and first_sentence_semantic_hit
            and visibility_mode == "objective_first"
            and not signal_tokens
            and any(token in {"見せ場", "勝負"} for token in objective_tokens)
        )
        timing_objective_only = (
            focus_family == "timing"
            and first_sentence_semantic_hit
            and visibility_mode == "objective_first"
            and not signal_tokens
            and any(token in {"流れ", "回収"} for token in objective_tokens)
        )
        basis_objective_only = (
            focus_family == "basis"
            and first_sentence_semantic_hit
            and visibility_mode == "objective_first"
            and not signal_tokens
            and any(token in {"流れ", "回収"} for token in objective_tokens)
        )
        timing_scene_priority_objective = (
            focus_family == "timing"
            and first_sentence_semantic_hit
            and any(token in {"見せ場"} for token in signal_tokens)
            and any(token in {"流れ", "回収"} for token in objective_tokens)
        )
        basis_dramatic_objective = (
            focus_family == "basis"
            and first_sentence_semantic_hit
            and visibility_mode == "objective_first"
            and any(token in {"見せ場", "勝負"} for token in objective_tokens)
        )
        generic_replacement_used = any(token in window_text for token in forbidden_generic_replacements)
        signal_any_visible, signal_visible = QualityGuard._visibility_anchor_state(window_text, signal_tokens)
        objective_any_visible, objective_visible = QualityGuard._visibility_anchor_state(window_text, objective_tokens)
        overlapping_visibility_tokens = bool(set(signal_tokens) & set(objective_tokens))
        if focus_family == "timing" and first_sentence_semantic_hit and overlapping_visibility_tokens:
            signal_visible = signal_any_visible or signal_visible
            objective_visible = objective_any_visible or objective_visible
        if (
            focus_family == "timing"
            and first_sentence_semantic_hit
            and objective_visible
            and any(token in {"見せ場", "流れ", "回収"} for token in signal_tokens)
        ):
            signal_visible = True
        if (
            focus_family == "timing"
            and first_sentence_semantic_hit
            and any(token in {"流れ", "回収"} for token in signal_tokens)
            and any(token in {"見せ場", "勝負"} for token in objective_tokens)
            and any(phrase in window_text for phrase in ("順番", "区切", "先に", "決め直"))
        ):
            signal_visible = True
            objective_visible = objective_any_visible or objective_visible
        if (
            focus_family == "decision_owner"
            and first_sentence_semantic_hit
            and objective_visible
            and any(token in {"流れ", "回収"} for token in signal_tokens)
            and any(phrase in window_text for phrase in ("流れ", "順番", "曖昧にしない", "決め直"))
        ):
            signal_visible = True
        if (
            focus_family == "timing"
            and first_sentence_semantic_hit
            and visibility_mode == "paired"
            and any(token in {"主導権", "入れ替わり"} for token in signal_tokens)
            and any(token in {"主導権", "入れ替わり"} for token in objective_tokens)
            and any(phrase in window_text for phrase in ("区切る", "区切", "ライン", "線引き", "先に"))
        ):
            return []
        if (
            focus_family == "decision_owner"
            and first_sentence_semantic_hit
            and visibility_mode == "objective_first"
            and any(token in {"流れ", "回収"} for token in objective_tokens)
            and any(token in window_text for token in ("流れ", "回収"))
            and any(token in window_text for token in ("主導権", "入れ替わり", "順番", "握る"))
        ):
            objective_visible = objective_any_visible or objective_visible
            lenient_visibility = True
        if (
            focus_family == "decision_owner"
            and first_sentence_semantic_hit
            and visibility_mode == "objective_first"
            and any(token in {"流れ", "回収"} for token in objective_tokens)
            and "見せ場" in window_text
            and "順番" in window_text
            and any(token in window_text for token in ("決め直", "定義", "今ここで"))
        ):
            return []
        if (
            focus_family == "premise"
            and first_sentence_semantic_hit
            and visibility_mode == "objective_first"
            and any(token in {"流れ", "回収"} for token in objective_tokens)
            and any(phrase in window_text for phrase in ("前提", "流れ", "順番", "決め直"))
        ):
            objective_visible = objective_any_visible or objective_visible
            lenient_visibility = True
        if focus_family == "basis" and first_sentence_semantic_hit and overlapping_visibility_tokens:
            if not signal_visible and not objective_visible:
                return []
            lenient_visibility = True
        if (
            focus_family == "basis"
            and first_sentence_semantic_hit
            and objective_visible
            and any(token in {"見せ場", "勝負", "流れ", "回収"} for token in signal_tokens)
        ):
            signal_visible = True
        if (
            basis_conflict_objective
            and objective_visible
            and any(token in {"主導権", "見せ場", "勝負", "流れ", "回収"} for token in signal_tokens)
            and any(phrase in window_text for phrase in ("食い違い", "解消", "潰", "片づけ", "ほどく", "決めて"))
        ):
            signal_visible = True
            lenient_visibility = True
        if basis_process_objective:
            lenient_visibility = True
        if timing_scene_priority_objective:
            return []
        if timing_dramatic_objective and any(
            phrase in window_text
            for phrase in ("流れ", "範囲", "区切", "切る", "境界", "先に決め", "先に提示")
        ):
            return []
        if timing_objective_only:
            return []
        if basis_objective_only and any(
            phrase in window_text
            for phrase in ("流れ", "回収", "切る", "区切る", "境界線", "判断基準", "何で測")
        ):
            return []
        if timing_control_objective:
            lenient_visibility = True
        if basis_control_objective:
            lenient_visibility = True
        if basis_dramatic_objective:
            lenient_visibility = True
        if (
            focus_family == "basis"
            and first_sentence_semantic_hit
            and any(token in {"流れ", "回収"} for token in signal_tokens)
            and any(token in {"主導権", "入れ替わり"} for token in objective_tokens)
        ):
            return []
        visible_count = int(signal_any_visible) + int(objective_any_visible)
        issues: list[dict[str, Any]] = []

        if signal_tokens and not signal_visible:
            severity = "warning" if visible_count == 0 and not lenient_visibility else "info"
            issues.append(
                {
                    "issue_type": "signal_visibility_missing",
                    "severity": severity,
                    "details": {
                        "reply_visibility_mode": visibility_mode,
                        "signal_anchor_tokens": signal_tokens,
                        "objective_anchor_tokens": objective_tokens,
                        "signal_visible": False,
                        "objective_visible": objective_visible,
                        "visibility_blocking": severity == "warning",
                        "reply_focus_family": focus_family or None,
                        "reply_focus_first_sentence_semantic_hit": first_sentence_semantic_hit,
                        "generic_replacement_used": generic_replacement_used,
                    },
                }
            )
        if objective_tokens and not objective_visible:
            if focus_family == "timing" and first_sentence_semantic_hit and (
                signal_visible or timing_control_objective
            ):
                return issues
            if focus_family == "basis" and first_sentence_semantic_hit and (
                signal_visible or basis_control_objective
            ):
                return issues
            if basis_dramatic_objective:
                return issues
            severity = "warning" if visible_count == 0 and not lenient_visibility else "info"
            issues.append(
                {
                    "issue_type": "scene_objective_visibility_missing",
                    "severity": severity,
                    "details": {
                        "reply_visibility_mode": visibility_mode,
                        "signal_anchor_tokens": signal_tokens,
                        "objective_anchor_tokens": objective_tokens,
                        "signal_visible": signal_visible,
                        "objective_visible": False,
                        "visibility_blocking": severity == "warning",
                        "reply_focus_family": focus_family or None,
                        "reply_focus_first_sentence_semantic_hit": first_sentence_semantic_hit,
                        "generic_replacement_used": generic_replacement_used,
                    },
                }
            )
        return issues

    @staticmethod
    def _visibility_anchor_state(text: str, tokens: list[str]) -> tuple[bool, bool]:
        if not tokens:
            return False, False
        generic_tokens = {"決める", "決め", "する", "話", "今", "あと", "ここ", "その"}
        specific_tokens = [token for token in tokens if token not in generic_tokens]
        any_visible = any(token in text for token in tokens)
        strong_visible = any(token in text for token in (specific_tokens or tokens))
        return any_visible, strong_visible

    @staticmethod
    def _reply_variety_second_beat_satisfied(
        second_sentence: str,
        *,
        second_beat_mode: str,
        preferred_move_tokens: list[str],
    ) -> bool:
        if not second_beat_mode:
            return True
        if preferred_move_tokens and any(token in second_sentence for token in preferred_move_tokens):
            return True
        if second_beat_mode == "press":
            return any(token in second_sentence for token in ("先に", "言って", "出して", "今", "決め", "返して"))
        if second_beat_mode == "condition":
            return any(token in second_sentence for token in ("なら", "そのあと", "先なら", "条件", "済んだら"))
        if second_beat_mode == "redirect":
            return any(token in second_sentence for token in ("じゃあ", "その前に", "まず", "代わりに", "別の"))
        return True

    @staticmethod
    def _infer_reply_second_beat_mode(second_sentence: str) -> str | None:
        if any(token in second_sentence for token in ("じゃあ", "その前に", "まず", "代わりに", "別の")):
            return "redirect"
        if any(token in second_sentence for token in ("なら", "そのあと", "先なら", "条件", "済んだら")):
            return "condition"
        if any(token in second_sentence for token in ("先に", "言って", "出して", "今", "決め", "返して", "出せ")):
            return "press"
        return None

    @staticmethod
    def _reply_dramatic_move_satisfied(
        second_sentence: str,
        *,
        move_mode: str,
        required_move_tokens: list[str],
        semantic_move_family: str,
        pressure_anchor_tokens: list[str],
        required_semantic_cues: list[str],
        forbidden_semantic_drifts: list[str],
    ) -> bool:
        if not move_mode:
            return True
        move_token_seen = False
        if required_move_tokens and any(token in second_sentence for token in required_move_tokens):
            move_token_seen = True
        elif move_mode == "counter":
            move_token_seen = any(token in second_sentence for token in ("先に", "言って", "出せ", "逃が", "曖昧"))
        elif move_mode == "condition":
            move_token_seen = any(token in second_sentence for token in ("なら", "そのあと", "条件", "先なら"))
        elif move_mode == "probe":
            move_token_seen = any(
                token in second_sentence
                for token in (*QualityGuard._probe_response_tokens(), "まだ", "本当に", "?", "？")
            )
        elif move_mode == "claim":
            move_token_seen = any(
                token in second_sentence
                for token in ("決める", "通す", "固定", "言い切る", "曖昧にしない", "終わらせない", "流さない")
            )
        semantic_seen = QualityGuard._reply_dramatic_semantic_family_satisfied(
            second_sentence,
            semantic_family=semantic_move_family or move_mode,
            required_semantic_cues=required_semantic_cues,
        )
        anchor_seen = (
            not pressure_anchor_tokens
            or any(token in second_sentence for token in pressure_anchor_tokens)
        )
        semantic_drift_used = any(token in second_sentence for token in forbidden_semantic_drifts)
        return move_token_seen and semantic_seen and anchor_seen and not semantic_drift_used

    @staticmethod
    def _reply_dramatic_semantic_family_satisfied(
        second_sentence: str,
        *,
        semantic_family: str,
        required_semantic_cues: list[str],
    ) -> bool:
        if required_semantic_cues and any(token in second_sentence for token in required_semantic_cues):
            return True
        if semantic_family == "counter":
            return any(token in second_sentence for token in ("違う", "先に", "まず", "出せ"))
        if semantic_family == "condition":
            return any(token in second_sentence for token in ("なら", "条件", "そのあと", "先なら"))
        if semantic_family == "probe":
            return any(token in second_sentence for token in (*QualityGuard._probe_response_tokens(), "？", "?"))
        if semantic_family == "claim":
            return any(
                token in second_sentence
                for token in (
                    "今決める",
                    "譲らない",
                    "私がやる",
                    "通す",
                    "固定",
                    "ここで決める",
                    "曖昧にしない",
                    "終わらせない",
                    "流さない",
                )
            )
        return True

    def _target_name_tokens(self, target_char_name: str | None) -> tuple[str, ...]:
        if not target_char_name:
            return ()
        compact = target_char_name.replace(" ", "")
        tokens = {target_char_name, compact}
        tokens.update(part for part in re.split(r"\s+", target_char_name) if part)
        if compact.startswith("ゆっくり") and len(compact) > len("ゆっくり"):
            tokens.add(compact[len("ゆっくり"):])
        trailing_segment = re.search(r"[ぁ-んァ-ヶー]+$", compact)
        if trailing_segment is not None:
            tokens.add(trailing_segment.group())
        return tuple(token for token in tokens if token)

    def _shares_dialogue_token(self, first_sentence: str, recent_dialogue_lines: list[str]) -> bool:
        source_text = " ".join(recent_dialogue_lines[-2:])
        source_tokens = {
            token
            for token in re.findall(r"[一-龠ぁ-んァ-ヶA-Za-z0-9]{2,}", source_text)
            if token not in _REACTION_STOP_WORDS
        }
        if not source_tokens:
            source_tokens = set()
        first_tokens = set(re.findall(r"[一-龠ぁ-んァ-ヶA-Za-z0-9]{2,}", first_sentence))
        if source_tokens & first_tokens:
            return True

        source_clean = re.sub(r"[^一-龠ぁ-んァ-ヶA-Za-z0-9]", "", source_text)
        first_clean = re.sub(r"[^一-龠ぁ-んァ-ヶA-Za-z0-9]", "", first_sentence)
        for size in (4, 3, 2):
            for index in range(0, max(len(first_clean) - size + 1, 0)):
                token = first_clean[index:index + size]
                if len(token) < 2 or token in _REACTION_STOP_WORDS:
                    continue
                if token in source_clean and re.search(r"[一-龠ァ-ヶA-Za-z0-9]", token):
                    return True
        return False

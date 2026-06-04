"""
engine/pre_turn_scene_engine.py — ターン前に演出家が演技指導ノートを生成する。

各ラウンド（または各ターン）の開始前に LLM を呼び出し、director payload を
構造化 JSON で生成して DB に保存する。生成された payload は管理画面用の
可読テキストへ render しつつ、発話生成ではキャラごとの実行カードとして利用する。

設計根拠: Stanislavski/Mamet の objective 理論 + McKee の turning point +
是枝/黒澤の物理的ブロッキング + negative space によるメタ語彙抑制
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from engine.structured_generation import StructuredJSONPolicy, generate_structured_json

if TYPE_CHECKING:
    from db.db_manager import DatabaseManager
    from engine.config import SceneScriptConfig
    from engine.llm.router import LLMRouter

logger = logging.getLogger(__name__)


# ============================================================
# SceneScriptContext — generate_for_turn() に渡す状況コンテキスト
# ============================================================

@dataclass
class SceneScriptContext:
    """PreTurnSceneEngine.generate_for_turn() に渡す状況コンテキスト。"""

    sim_datetime: str                                         # "YYYY-MM-DDTHH:MM"
    world_rules: str                                          # stories.world_rules
    recent_dialogue_lines: list[str] = field(default_factory=list)  # 直近の会話ログ
    active_character_states: list[dict[str, Any]] = field(default_factory=list)
    # 各 dict: {char_id, name, place, current_goal, current_worry, dominant_emotion}
    required_character_ids: list[str] = field(default_factory=list)
    current_places: dict[str, list[str]] = field(default_factory=dict)  # place_label→char_names
    chapter_world_injection: str | None = None
    chapter_beat_goal: str | None = None
    chapter_beat_phase: str | None = None
    chapter_id: str | None = None
    director_persona_name: str | None = None
    director_persona_values: list[str] = field(default_factory=list)
    director_persona_traits: list[str] = field(default_factory=list)
    director_persona_id: str | None = None
    round_number: int | None = None


# ============================================================
# PreTurnSceneEngine
# ============================================================

class PreTurnSceneEngine:
    """ラウンド/ターン開始前に演技指導ノートを生成し DB に保存する。"""

    def __init__(
        self,
        story_id: str,
        db: DatabaseManager,
        llm_router: LLMRouter,
        config: SceneScriptConfig,
        llm_provider: str = "",
        llm_model: str = "",
    ) -> None:
        self._story_id = story_id
        self._db = db
        self._llm_router = llm_router
        self._config = config
        self._llm_provider = llm_provider
        self._llm_model = llm_model
        self._last_generated_turn: int = -1
        self._cached_script: str | None = None
        self._pending_generation_metadata: dict[str, Any] | None = None
        self._cached_generation_metadata: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # 公開 API
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """DB から最新スクリプトを取得してキャッシュを復元する（再起動対応）。"""
        latest = await self._db.get_latest_scene_script(self._story_id)
        if latest is not None:
            self._last_generated_turn = int(latest.get("turn_number") or -1)
            self._cached_script = str(latest.get("script_text", ""))
            metadata = latest.get("generation_metadata")
            self._cached_generation_metadata = metadata if isinstance(metadata, dict) else None
        logger.info(
            "PreTurnSceneEngine initialized",
            extra={
                "story_id": self._story_id,
                "last_generated_turn": self._last_generated_turn,
            },
        )

    async def generate_for_turn(
        self,
        turn_number: int,
        ctx: SceneScriptContext,
    ) -> str | None:
        """指定ターンの演技指導ノートを生成して DB に保存し、テキストを返す。

        generation_mode == "per_round" の場合、直前のターンと同一ラウンドなら
        キャッシュを返す（ラウンド先頭のみ生成）。

        Returns:
            生成された演技指導ノート、生成しない場合は None。
        """
        if not self._config.enabled:
            return None

        if self._config.generation_mode == "per_round" and self._cached_script is not None:
            if turn_number == self._last_generated_turn:
                return self._cached_script

        text = await self._generate(turn_number, ctx)
        if not text:
            return self._cached_script

        await self._save(text, turn_number, ctx)
        self._last_generated_turn = turn_number
        self._cached_script = text
        logger.info(
            "Scene script generated",
            extra={"story_id": self._story_id, "turn_number": turn_number},
        )
        return text

    def get_cached_script(self) -> str | None:
        """最後に生成したスクリプトを返す（キャラ context 充填用）。"""
        return self._cached_script

    def get_cached_generation_metadata(self) -> dict[str, Any] | None:
        """最後に生成した structured metadata を返す。"""
        return self._cached_generation_metadata

    def clear_cache(self) -> None:
        """キャッシュを破棄する（仕切り直し後などに使用）。"""
        self._cached_script = None
        self._cached_generation_metadata = None
        self._last_generated_turn = -1

    # ------------------------------------------------------------------
    # 内部実装
    # ------------------------------------------------------------------

    async def _generate(self, turn_number: int, ctx: SceneScriptContext) -> str:
        """LLM を呼び出してスクリプトテキストを生成する。"""
        previous_scripts = await self._db.get_recent_scene_scripts(
            self._story_id,
            limit=max(1, self._config.reference_previous_n),
        )
        system_prompt = self._build_system_prompt(ctx)
        user_prompt = self._build_user_prompt(turn_number, ctx, previous_scripts)
        required_char_ids = _required_character_ids(
            ctx,
            max_directive_characters=self._config.max_directive_characters,
        )
        known_char_ids = _active_character_ids(ctx.active_character_states)
        try:
            result = await generate_structured_json(
                router=self._llm_router,
                provider=self._llm_provider,
                model=self._llm_model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                parser=lambda text: _parse_director_payload(
                    text,
                    required_char_ids=known_char_ids,
                ),
                repair_schema_prompt=_DIRECTOR_PAYLOAD_SCHEMA_PROMPT,
                policy=StructuredJSONPolicy(
                    name=f"scene_script_turn_{turn_number}",
                    response_max_tokens=self._config.response_max_tokens,
                    repair_max_tokens=self._config.repair_max_tokens,
                    temperature=0.35,
                    reasoning_mode="off",
                    repair_reasoning_mode="off",
                ),
            )
            payload = result.parsed
            if payload is not None:
                payload, auto_filled_ids = _ensure_required_character_cards(
                    payload,
                    ctx,
                    required_char_ids,
                )
                self._pending_generation_metadata = {
                    "director_payload": payload,
                    "required_character_ids": required_char_ids,
                    "auto_filled_character_ids": auto_filled_ids,
                    "max_directive_characters": self._config.max_directive_characters,
                }
                return _render_director_payload(payload)
            self._pending_generation_metadata = None
            logger.warning(
                "Scene script structured payload rejected",
                extra={
                    "story_id": self._story_id,
                    "turn_number": turn_number,
                    "parser_error": result.parser_error,
                    "done_reason": result.done_reason,
                    "used_repair": result.used_repair,
                },
            )
            return ""
        except Exception:
            logger.warning(
                "Scene script generation failed",
                extra={"story_id": self._story_id, "turn_number": turn_number},
                exc_info=True,
            )
            return ""

    def _build_system_prompt(self, ctx: SceneScriptContext) -> str:
        """演出家（俳優指導）用システムプロンプトを構築する。"""
        world_rules = _compact(ctx.world_rules, 300)

        persona_section = ""
        if ctx.director_persona_name:
            values_text = "、".join(ctx.director_persona_values[:3]) if ctx.director_persona_values else ""
            traits_text = "、".join(ctx.director_persona_traits[:3]) if ctx.director_persona_traits else ""
            persona_section = (
                f"\n【演出スタイル】\n"
                f"演出家: {ctx.director_persona_name}"
                + (f"（重視する価値: {values_text}）" if values_text else "")
                + (f"\n演出の特徴: {traits_text}" if traits_text else "")
                + "\nこのスタイルに従って director payload の密度と温度感を調整してください。\n"
            )

        few_shot = _FEW_SHOT_DIRECTORIAL_JSON_EXAMPLE

        return (
            "あなたは舞台・映画の演出家です。\n"
            "俳優（キャラクター）に次の 1 ターン分の演技指導 plan を JSON で返してください。\n"
            "\n"
            "【重要ルール】\n"
            "- JSON 以外を返してはいけません\n"
            "- 台詞本文を書いてはいけません\n"
            "- scene_frame は見える・聞こえる・触れるものを含む 1〜2 文の場面要約にしてください\n"
            "- characters は場面で演出指示が必要なキャラの内部実行カードです。口に出す文ではなく、発話内容の狙いを書いてください\n"
            "- characters の key はキャラクタープロファイルにある char_id と完全一致させてください\n"
            "- 各 character card には name / role / next_move / speech_task / actable_behavior / target_char_id / avoid を入れてください\n"
            "- banned_surface_patterns には台詞として避けたい定型句やメタ語彙を入れてください\n"
            "\n"
            "【JSON schema】\n"
            '{"scene_frame":"...","turn_goal":"...","turn_shift":"...",'
            '"banned_surface_patterns":["..."],'
            '"characters":{"char_id":{"name":"...","role":"...","next_move":"...",'
            '"speech_task":"...","actable_behavior":"...","target_char_id":"...","avoid":["..."]}}}\n'
            "\n"
            f"{persona_section}"
            "【世界設定】\n"
            f"{world_rules}\n"
            "\n"
            "【出力例（形式の参考）】\n"
            f"{few_shot}"
        )

    def _build_user_prompt(
        self,
        turn_number: int,
        ctx: SceneScriptContext,
        previous_scripts: list[dict[str, Any]],
    ) -> str:
        """状況情報を積んだユーザープロンプトを構築する。"""
        lines: list[str] = []

        lines.append(f"【現在の日時】\n{ctx.sim_datetime}")

        if previous_scripts:
            prev_text = previous_scripts[0].get("script_text", "")
            if prev_text:
                lines.append("")
                lines.append("【直前の演技指導ノート】")
                lines.append(_compact(prev_text, 300))

        if ctx.chapter_world_injection or ctx.chapter_beat_goal:
            lines.append("")
            lines.append("【現在の章の状況】")
            if ctx.chapter_world_injection:
                lines.append(_compact(ctx.chapter_world_injection, 150))
            if ctx.chapter_beat_phase:
                lines.append(f"ビートフェーズ: {ctx.chapter_beat_phase}")
            if ctx.chapter_beat_goal:
                lines.append(f"この場面の方向性: {_compact(ctx.chapter_beat_goal, 80)}")

        if ctx.active_character_states:
            lines.append("")
            lines.append("【キャラクタープロファイル（演出の参考）】")
            for cs in ctx.active_character_states:
                char_id = cs.get("char_id", "")
                name = cs.get("name", "")
                place = cs.get("place", "")
                goal = cs.get("current_goal", "")
                worry = cs.get("current_worry", "")
                emotion = cs.get("dominant_emotion", "")
                id_text = f" char_id:{char_id}" if char_id else ""
                line = f"・{name}（{place}）{id_text}"
                if goal:
                    line += f" 目標:{_compact(goal, 40)}"
                if worry:
                    line += f" 悩み:{_compact(worry, 40)}"
                if emotion:
                    line += f" 感情:{emotion}"
                lines.append(line)

        required_ids = _required_character_ids(
            ctx,
            max_directive_characters=self._config.max_directive_characters,
        )
        if required_ids:
            profile_by_id = _character_state_by_id(ctx.active_character_states)
            lines.append("")
            lines.append("【このターンで必ず character card を作る発話予定キャラ】")
            for char_id in required_ids:
                state = profile_by_id.get(char_id, {})
                name = str(state.get("name") or char_id).strip()
                lines.append(f"・{name} char_id:{char_id}")
            lines.append(
                f"characters には上記を最大 {self._config.max_directive_characters} 人まで必ず含めてください。"
            )

        if ctx.recent_dialogue_lines:
            lines.append("")
            lines.append("【直近の会話（最新 3 行）】")
            for line in ctx.recent_dialogue_lines[-3:]:
                lines.append(_compact(line, 80))

        lines.append("")
        lines.append(
            "上記の状況を踏まえて、次のターンの director payload を JSON だけで返してください。"
        )

        return "\n".join(lines)

    async def _save(
        self, text: str, turn_number: int, ctx: SceneScriptContext
    ) -> None:
        """生成したスクリプトを DB に保存する。"""
        generation_metadata = self._pending_generation_metadata
        await self._db.insert_scene_script(
            self._story_id,
            {
                "turn_number": turn_number,
                "round_number": ctx.round_number,
                "script_text": text,
                "director_persona_id": ctx.director_persona_id,
                "chapter_id": ctx.chapter_id,
                "beat_phase": ctx.chapter_beat_phase,
                "generation_mode": self._config.generation_mode,
                "format_mode": self._config.format_mode,
                "generation_metadata": generation_metadata,
                "llm_provider": self._llm_provider,
                "llm_model": self._llm_model,
            },
        )
        self._cached_generation_metadata = generation_metadata
        self._pending_generation_metadata = None


# ============================================================
# few-shot 例（14B 級モデルの指示追従性向上のため埋め込み）
# ============================================================

_FEW_SHOT_DIRECTORIAL_JSON_EXAMPLE = """\
{
  "scene_frame": "廊下の蛍光灯が一本切れ、窓の外で雨音が途切れず続く。",
  "turn_goal": "ルナの手帳とユウマの楽譜の間にある緊張を具体化する。",
  "turn_shift": "ちゅるるんが一歩前に出て、二人の間へ視線を割り込ませる。",
  "banned_surface_patterns": ["どうするの？", "決める", "順番", "主導権"],
  "characters": {
    "hoshikaze_runa": {
      "name": "星風ルナ",
      "role": "秘密を守る側",
      "next_move": "ユウマとちゅるるんの視線を手帳からそらす",
      "speech_task": "手帳を渡せない理由をぼかして返す",
      "actable_behavior": "手帳の角を胸元へ寄せる",
      "target_char_id": "chururun",
      "avoid": ["直接命令", "抽象的な問い"]
    }
  }
}
"""

_DIRECTOR_PAYLOAD_SCHEMA_PROMPT = (
    '{"scene_frame":"...",'
    '"turn_goal":"...",'
    '"turn_shift":"...",'
    '"banned_surface_patterns":["..."],'
    '"characters":{"char_id":{"name":"...",'
    '"role":"...",'
    '"next_move":"...",'
    '"speech_task":"...",'
    '"actable_behavior":"...",'
    '"target_char_id":"...",'
    '"avoid":["..."]}}}'
)


# ============================================================
# ユーティリティ
# ============================================================

def _compact(text: str, max_chars: int) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "").strip())
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 1].rstrip() + "…"


def _active_character_ids(active_character_states: list[dict[str, Any]]) -> set[str]:
    return {
        str(item.get("char_id") or "").strip()
        for item in active_character_states
        if str(item.get("char_id") or "").strip()
    }


def _character_state_by_id(active_character_states: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("char_id") or "").strip(): item
        for item in active_character_states
        if str(item.get("char_id") or "").strip()
    }


def _required_character_ids(
    ctx: SceneScriptContext,
    *,
    max_directive_characters: int,
) -> list[str]:
    cap = max(1, int(max_directive_characters or 10))
    source = ctx.required_character_ids or [
        str(item.get("char_id") or "").strip()
        for item in ctx.active_character_states
    ]
    result: list[str] = []
    seen: set[str] = set()
    for char_id in source:
        cleaned = str(char_id or "").strip()
        if not cleaned or cleaned in seen:
            continue
        result.append(cleaned)
        seen.add(cleaned)
        if len(result) >= cap:
            break
    return result


def _ensure_required_character_cards(
    payload: dict[str, Any],
    ctx: SceneScriptContext,
    required_char_ids: list[str],
) -> tuple[dict[str, Any], list[str]]:
    characters = payload.get("characters")
    if not isinstance(characters, dict):
        payload["characters"] = {}
        characters = payload["characters"]
    profile_by_id = _character_state_by_id(ctx.active_character_states)
    auto_filled: list[str] = []
    for char_id in required_char_ids:
        if char_id in characters and isinstance(characters[char_id], dict):
            continue
        state = profile_by_id.get(char_id, {})
        characters[char_id] = _build_default_character_card(char_id, state)
        auto_filled.append(char_id)
    return payload, auto_filled


def _build_default_character_card(char_id: str, state: dict[str, Any]) -> dict[str, Any]:
    name = str(state.get("name") or char_id).strip()
    goal = str(state.get("current_goal") or "").strip()
    worry = str(state.get("current_worry") or "").strip()
    place = str(state.get("place") or "").strip()
    role = "場面に短く反応する"
    if goal:
        role = f"{goal}を抱えて場面に反応する"
    speech_task = "直前の相手発話か場面の具体物に触れて、自分の立場を短く返す"
    if worry:
        speech_task = f"{worry}をにじませつつ、直前の相手発話か場面の具体物に短く返す"
    behavior = "近くの具体物へ視線を向ける"
    if place:
        behavior = f"{place}にある具体物へ視線を向ける"
    return {
        "name": name,
        "role": role,
        "next_move": "会話の直前の一点に自然に反応する",
        "speech_task": speech_task,
        "actable_behavior": behavior,
        "target_char_id": "",
        "avoid": [
            "残った所を言葉にする",
            "流れをどうする",
            "内部指示を台詞にする",
        ],
    }


def _extract_json_payload_text(text: str) -> str:
    cleaned = str(text or "").strip()
    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, flags=re.DOTALL)
    if fence_match:
        cleaned = fence_match.group(1).strip()
    return cleaned


def _parse_director_payload(
    text: str,
    *,
    required_char_ids: set[str] | None = None,
) -> dict[str, Any] | None:
    """structured scene script JSON を dict として読む。"""
    cleaned = _extract_json_payload_text(text)
    if not cleaned.startswith("{"):
        return None
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    if not isinstance(parsed.get("characters"), dict):
        return None
    character_cards = parsed["characters"]
    if not character_cards:
        return None
    if any(
        not isinstance(char_id, str)
        or not char_id.strip()
        or not isinstance(card, dict)
        for char_id, card in character_cards.items()
    ):
        return None
    required_ids = {char_id for char_id in (required_char_ids or set()) if char_id}
    if required_ids:
        unknown_ids = set(character_cards) - required_ids
        if unknown_ids:
            return None
    return parsed


def _render_director_payload(payload: dict[str, Any]) -> str:
    """structured scene script を管理画面向け表示文に整形する。"""
    characters = payload.get("characters") if isinstance(payload.get("characters"), dict) else {}
    lines: list[str] = [
        "〔シーン〕",
        str(payload.get("scene_frame") or "").strip() or "場の空気が張りつめている。",
        "〔ターン目的〕",
        str(payload.get("turn_goal") or "").strip() or "場面を一歩前に進める。",
        "〔転機〕",
        str(payload.get("turn_shift") or "").strip() or "空気の向きが少し変わる。",
        "〔各キャラの実行カード〕",
    ]
    for directive in characters.values():
        if not isinstance(directive, dict):
            continue
        name = str(directive.get("name") or "").strip() or "キャラクター"
        role = str(directive.get("role") or "").strip()
        next_move = str(directive.get("next_move") or "").strip()
        speech_task = str(directive.get("speech_task") or "").strip()
        behavior = str(directive.get("actable_behavior") or "").strip()
        avoid = directive.get("avoid") if isinstance(directive.get("avoid"), list) else []
        parts = [part for part in [f"役割:{role}" if role else "", f"動き:{next_move}" if next_move else "", f"発話:{speech_task}" if speech_task else "", f"所作:{behavior}" if behavior else ""] if part]
        line = f"・{name}: " + " / ".join(parts or ["場面に即して短く反応する"])
        if avoid:
            line += f" / 避ける:{'・'.join(str(item) for item in avoid if str(item).strip())}"
        lines.append(line)
    lines.append("〔禁止〕")
    banned = payload.get("banned_surface_patterns")
    if isinstance(banned, list) and banned:
        lines.extend(f"- {str(item)}" for item in banned if str(item).strip())
    else:
        lines.append("- メタ語彙や空疎な問いを台詞にしない")
    return "\n".join(lines)




# directorial モードで〔〕section header を含む構造化テキストを判定
_DIRECTORIAL_MARKERS = ("〔シーン〕", "〔配置〕", "〔各キャラの今ここ〕", "〔転機〕", "〔身体〕", "〔禁止〕")


def _clean_script_text(text: str, config: "SceneScriptConfig") -> str:
    """LLM 出力を整形する。

    directorial モード: 〔〕セクション構造を保持し、max_chars でハードカット。
    narrative モード: 旧来の文数カウントロジックを使用（後方互換）。
    """
    text = text.strip()
    if not text:
        return ""

    is_directorial = (
        config.format_mode == "directorial"
        or any(marker in text for marker in _DIRECTORIAL_MARKERS)
    )

    if is_directorial:
        # 先頭に余分なプレフィックス（"以下は演技指導ノートです。" 等）が付く場合を除去。
        # テキスト内で最初に出現するセクションマーカーを見つけて、それより前を切り落とす。
        first_pos: int | None = None
        for marker in _DIRECTORIAL_MARKERS:
            idx = text.find(marker)
            if idx >= 0 and (first_pos is None or idx < first_pos):
                first_pos = idx
        if first_pos is not None and first_pos > 0:
            text = text[first_pos:]
        # 800 字でハードカット（〔〕セクション全体を保持するため sentence split しない）
        max_chars = 800
        if len(text) > max_chars:
            # 直近の改行境界で切る
            cut = text[:max_chars].rfind("\n")
            text = text[: cut if cut > 0 else max_chars] + "…"
        return text

    # narrative モード（後方互換）
    max_sentences = getattr(config, "max_sentences", 3)
    sentences = re.split(r"(?<=[。！？!?])\s*", text)
    sentences = [s.strip() for s in sentences if s.strip()]
    if len(sentences) > max_sentences:
        sentences = sentences[:max_sentences]
    result = "".join(s if s.endswith(("。", "！", "？", "!", "?")) else s + "。" for s in sentences)
    return result.strip("。") + "。" if result and not result.endswith(("。", "！", "？", "!", "?")) else result

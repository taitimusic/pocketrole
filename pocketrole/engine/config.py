"""
engine/config.py — 設定読み込み・バリデーション・dataclass定義

使い方:
    from engine.config import load_config, Config
    cfg = load_config("config.yaml", ".env")
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

# ============================================================
# エラークラス
# ============================================================

class ConfigError(Exception):
    """設定関連エラーの基底クラス。"""


class ConfigFileNotFoundError(ConfigError):
    """config.yaml が見つからない場合。"""


class ConfigValidationError(ConfigError):
    """YAML の内容が不正な場合。"""


class LLMProviderNotConfiguredError(ConfigError):
    """指定されたプロバイダーが有効化されていない場合。"""


class APIKeyMissingError(ConfigError):
    """クラウドプロバイダーが enabled=true だが APIキーが未設定の場合。"""


# ============================================================
# dataclass 定義
# ============================================================

VALID_PROVIDERS = frozenset({"ollama", "openai", "anthropic", "gemini", "deepseek"})

# クラウドプロバイダーと対応する .env キー名のマッピング
_CLOUD_API_KEY_MAP: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}
_VALID_OPENAI_API_MODES = frozenset({"auto", "chat_completions", "responses"})


@dataclass
class OllamaProviderConfig:
    base_url: str
    default_model: str
    timeout_sec: int
    max_retries: int


@dataclass
class ModelProfileConfig:
    reasoning_mode: str = "auto"
    max_tokens: int | None = None
    recovery_max_tokens: int | None = None
    temperature: float | None = None
    recovery_temperature: float | None = None
    recovery_reasoning_mode: str | None = None


@dataclass
class CloudProviderConfig:
    default_model: str
    timeout_sec: int
    max_retries: int
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    api_key: str = ""
    api_mode: Optional[str] = None


@dataclass
class LLMConfig:
    cloud_concurrency: int
    default_provider: str = ""
    default_model: str = ""
    providers: dict[str, Any] = field(default_factory=dict)
    model_profiles: dict[str, dict[str, ModelProfileConfig]] = field(default_factory=dict)


@dataclass
class EngineConfig:
    memory_limit: int
    emotion_decay_rate: float
    max_move_cost: int


@dataclass
class WebPosterSettingsConfig:
    batch_size: int
    retry_interval_sec: int
    max_consecutive_failures: int
    pause_duration_sec: int


@dataclass
class StoryWebPostTargetConfig:
    enabled: bool
    receiver_url: str
    auth_token: str


@dataclass
class LLMRuntimeProfileConfig:
    provider: str
    model: str


@dataclass
class LLMRuntimeConfig:
    active_profile: str = ""
    profiles: dict[str, LLMRuntimeProfileConfig] = field(default_factory=dict)
    story_overrides: dict[str, str] = field(default_factory=dict)


@dataclass
class WebPosterConfig:
    receiver_url: str
    batch_size: int
    retry_interval_sec: int
    max_consecutive_failures: int
    pause_duration_sec: int
    auth_token: str


@dataclass
class LoggingConfig:
    level: str
    log_dir: str
    rotation: str
    retention_days: int


@dataclass
class StoryMemoryConfig:
    """ストーリーメモリ設定。"""
    enabled: bool = False
    summarize_interval_turns: int = 10
    max_injection_count: int = 5
    importance_threshold: float = 0.3
    response_max_tokens: int = 220
    repair_max_tokens: int = 320


@dataclass
class NarrationConfig:
    """ナレーション設定。"""
    enabled: bool = False
    min_interval_turns: int = 3
    emotion_change_threshold: float = 0.3
    include_chapter_breaks: bool = True
    response_max_tokens: int = 480
    retry_max_tokens: int = 360
    chapter_max_tokens: int = 120


@dataclass
class CharacterEvolutionConfig:
    """キャラクター動的進化設定。"""
    enabled: bool = False
    check_interval_rounds: int = 1
    max_changes_per_check: int = 2


@dataclass
class StoryDirectorConfig:
    """ストーリー演出家設定。"""
    enabled: bool = False
    analysis_interval_rounds: int = 3
    max_active_interventions: int = 3
    max_active_tensions: int = 5
    tension_escalation_rounds: int = 5
    stagnation_detection: bool = True
    intervention_strength: str = "moderate"
    min_intervention_duration_rounds: int = 3
    repeat_intervention_cooldown_rounds: int = 4
    max_same_scope_active_interventions: int = 1


@dataclass
class NovelGeneratorConfig:
    """小説自動生成設定。"""
    enabled: bool = False
    generate_on_session_end: bool = True
    generate_on_chapter_break: bool = True
    response_max_tokens: int = 320
    repair_max_tokens: int = 480


@dataclass
class AmbientContextConfig:
    """短期環境コンテキスト設定。"""
    enabled: bool = False
    min_duration_turns: int = 3
    max_duration_turns: int = 8
    max_active_body_factors: int = 1
    max_active_place_factors: int = 1
    max_prompt_items: int = 3
    llm_phrase_enabled: bool = True


@dataclass
class SceneManagementConfig:
    enabled: bool = False
    max_active_scenes_per_story: int = 3


@dataclass
class ParticipationPlannerConfig:
    enabled: bool = False
    max_focus_characters: int = 2
    max_support_characters: int = 2
    monologue_solo_rate: float = 0.0    # 1:1 状況でのモノローグ注入確率
    monologue_group_rate: float = 0.0   # グループ状況でのモノローグ注入確率
    advance_monologue_bonus: float = 0.0  # advance intent キャラへの追加確率
    max_place_dialogue_lines: int = 10  # 滞在中に参照する会話履歴の最大件数（SLM向けデフォルト）


@dataclass
class StoryHooksConfig:
    enabled: bool = False
    max_open_hooks: int = 10


@dataclass
class CharacterDrivesConfig:
    enabled: bool = False
    max_taboo_topics: int = 3


@dataclass
class RelationshipDynamicsConfig:
    enabled: bool = False
    trust_delta_scale: float = 0.1


@dataclass
class QualityGuardConfig:
    enabled: bool = False
    max_group_chars: int = 180
    max_reply_chars: int = 140
    max_monologue_chars: int = 110
    max_current_affairs_chars: int = 180    # URL 込みのため長め
    max_abstract_token_occurrences: int = 3
    max_repeated_ngram_occurrences: int = 2
    max_sentences: int = 2  # 1 発話あたりの最大文数（。！？で分割）


@dataclass
class GrowthEngineConfig:
    enabled: bool = False
    rolling_window_turns: int = 24
    immediate_commit_identity_threshold: float = 0.80
    goal_worry_aggregate_commit_threshold: float = 0.70
    personality_core_aggregate_commit_threshold: float = 1.30
    personality_core_min_durable_support: int = 2
    rich_evidence_min_items: int = 3
    pending_candidate_ttl_turns: int = 24
    response_max_tokens: int = 260
    repair_max_tokens: int = 360


@dataclass
class EpisodePlannerConfig:
    enabled: bool = True
    min_turns_per_episode: int = 4
    max_turns_per_episode: int = 12
    carry_hook_limit: int = 3
    stale_rounds_before_close: int = 2


@dataclass
class DramaticPressureConfig:
    enabled: bool = True
    recent_window_turns: int = 8
    min_pressure_score: float = 0.55
    stale_rounds_before_resolve: int = 2
    max_active_pressures: int = 12


@dataclass
class ChapterConfig:
    """Chapter System 設定（v2 upgrade 追加）。"""
    enabled: bool = False
    max_active_chapters: int = 1
    beat_check_interval_rounds: int = 1
    beat_timeout_turns: int = 30
    auto_event_injection: bool = True
    # Phase 3 optional: LLM-assisted beat 進行判定
    enable_llm_beat_judgment: bool = False
    llm_beat_min_elapsed_turns: int = 5


@dataclass
class DirectorPersonaConfig:
    """Director Persona 設定（v2 upgrade 追加）。"""
    enabled: bool = False
    evaluation_interval_rounds: int = 3
    steering_strength: str = "moderate"  # 'subtle' / 'moderate' / 'strong'
    satisfaction_decay: float = 0.1
    allow_mid_chapter_swap: bool = True
    swap_cooldown_turns: int = 0
    preserve_satisfaction_on_swap: bool = True
    # Phase 3 optional: LLM-assisted 満足度評価
    enable_llm_evaluation: bool = False
    llm_eval_interval_rounds: int = 3


@dataclass
class ChapterGeneratorConfig:
    """Chapter Generator 設定（Phase 4 半自動 Chapter 生成）。"""
    enabled: bool = False
    default_beat_timeout: int = 20      # 生成 chapter のデフォルト beat タイムアウト
    include_conflict_seeds: bool = True  # conflict seed を LLM プロンプトに含める
    max_events_per_beat: int = 2         # 1 beat あたりのイベント上限


@dataclass
class SceneScriptConfig:
    """Pre-turn シーンスクリプト設定。監督ペルソナが各ターン前に情景を生成する。"""
    enabled: bool = False
    generation_mode: str = "per_round"  # "per_turn" | "per_round"
    format_mode: str = "directorial"    # "directorial"（推奨）| "narrative"（旧式）
    injection_mode: str = "header"      # "header" | "parallel"
    max_sentences: int = 3
    reference_previous_n: int = 1       # 直前 N 個のスクリプトを参照
    response_max_tokens: int = 720
    repair_max_tokens: int = 960
    max_directive_characters: int = 10


@dataclass
class NewsModeIntensityPreset:
    """発火率プリセット 1 件（off / low / medium / high）。"""
    rate: float = 0.0
    cooldown_turns: int = 999


@dataclass
class NewsModeConfig:
    """時事モード設定（RSS 巡回 + キャラが時事ネタをふらっと語る）。"""
    enabled: bool = False
    fetch_interval_sec: int = 1800
    http_timeout_sec: int = 30
    user_agent: str = "PocketRole/1.0 (+rss)"
    allow_private_feed_hosts: bool = False
    max_feed_bytes: int = 2_097_152
    max_entries_per_feed: int = 50
    max_articles_kept: int = 1000
    candidates_for_llm: int = 8
    intensity_presets: dict[str, NewsModeIntensityPreset] = field(
        default_factory=lambda: {
            "off":    NewsModeIntensityPreset(rate=0.0,  cooldown_turns=999),
            "low":    NewsModeIntensityPreset(rate=0.03, cooldown_turns=8),
            "medium": NewsModeIntensityPreset(rate=0.07, cooldown_turns=5),
            "high":   NewsModeIntensityPreset(rate=0.12, cooldown_turns=3),
            "rate20": NewsModeIntensityPreset(rate=0.20, cooldown_turns=3),
            "rate30": NewsModeIntensityPreset(rate=0.30, cooldown_turns=3),
            "rate40": NewsModeIntensityPreset(rate=0.40, cooldown_turns=3),
            "rate50": NewsModeIntensityPreset(rate=0.50, cooldown_turns=3),
        }
    )

    def get_preset(self, name: str) -> NewsModeIntensityPreset:
        return self.intensity_presets.get(name, self.intensity_presets["low"])


@dataclass
class Config:
    llm: LLMConfig
    engine: EngineConfig
    web_poster: WebPosterSettingsConfig
    web_post_targets: dict[str, StoryWebPostTargetConfig]
    logging: LoggingConfig
    llm_runtime: LLMRuntimeConfig = field(default_factory=LLMRuntimeConfig)
    story_memory: StoryMemoryConfig = field(default_factory=StoryMemoryConfig)
    narration: NarrationConfig = field(default_factory=NarrationConfig)
    character_evolution: CharacterEvolutionConfig = field(default_factory=CharacterEvolutionConfig)
    story_director: StoryDirectorConfig = field(default_factory=StoryDirectorConfig)
    novel_generator: NovelGeneratorConfig = field(default_factory=NovelGeneratorConfig)
    ambient_context: AmbientContextConfig = field(default_factory=AmbientContextConfig)
    scene_management: SceneManagementConfig = field(default_factory=SceneManagementConfig)
    participation_planner: ParticipationPlannerConfig = field(
        default_factory=ParticipationPlannerConfig
    )
    story_hooks: StoryHooksConfig = field(default_factory=StoryHooksConfig)
    character_drives: CharacterDrivesConfig = field(default_factory=CharacterDrivesConfig)
    relationship_dynamics: RelationshipDynamicsConfig = field(
        default_factory=RelationshipDynamicsConfig
    )
    quality_guard: QualityGuardConfig = field(default_factory=QualityGuardConfig)
    growth_engine: GrowthEngineConfig = field(default_factory=GrowthEngineConfig)
    episode_planner: EpisodePlannerConfig = field(default_factory=EpisodePlannerConfig)
    dramatic_pressure: DramaticPressureConfig = field(default_factory=DramaticPressureConfig)
    chapter: ChapterConfig = field(default_factory=ChapterConfig)
    director_persona: DirectorPersonaConfig = field(default_factory=DirectorPersonaConfig)
    chapter_generator: ChapterGeneratorConfig = field(default_factory=ChapterGeneratorConfig)
    scene_script: SceneScriptConfig = field(default_factory=SceneScriptConfig)
    news_mode: NewsModeConfig = field(default_factory=NewsModeConfig)


# ============================================================
# 公開関数
# ============================================================

def load_config(
    config_path: str = "config.yaml",
    env_path: str = ".env",
    web_post_targets_path: str = "config/web_post_targets.local.yaml",
    llm_runtime_path: str = "config/llm_runtime.local.yaml",
) -> Config:
    """
    設定ファイルを読み込み、バリデーションして Config を返す。

    Args:
        config_path: config.yaml のパス（デフォルト: "config.yaml"）
        env_path:    .env のパス（デフォルト: ".env"、存在しない場合はエラーにならない）
        web_post_targets_path:
                    story ごとの Web 投稿設定ファイルのパス

    Returns:
        Config dataclass インスタンス

    Raises:
        ConfigFileNotFoundError:       config.yaml が存在しない
        ConfigValidationError:         YAML の内容が不正
        APIKeyMissingError:            クラウドプロバイダーの APIキーが未設定
    """
    logger.info("設定ファイルを読み込みます: config_path=%s, env_path=%s", config_path, env_path)

    yaml_cfg = _load_yaml(config_path)
    env_cfg = _load_env(env_path)
    web_post_targets_cfg = _load_web_post_targets(web_post_targets_path)
    llm_runtime_cfg = _load_llm_runtime(llm_runtime_path)
    _validate(yaml_cfg, env_cfg, llm_runtime_cfg)
    cfg = _build(yaml_cfg, env_cfg, web_post_targets_cfg, llm_runtime_cfg)

    logger.info(
        "設定の読み込みが完了しました: configured_providers=%s",
        sorted(cfg.llm.providers.keys()),
    )
    return cfg


# ============================================================
# 内部関数
# ============================================================

def _load_yaml(config_path: str) -> dict:
    """config.yaml を読み込んで dict を返す。"""
    path = Path(config_path)
    if not path.exists():
        raise ConfigFileNotFoundError(
            f"config.yaml が見つかりません: {config_path}\n"
            "config.yaml.example をコピーして config.yaml を作成してください。"
        )

    try:
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise ConfigValidationError(f"config.yaml の YAML 構文エラー: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigValidationError("config.yaml のトップレベルはマッピング（dict）である必要があります。")

    logger.debug("config.yaml を読み込みました: keys=%s", list(data.keys()))
    return data


def _load_env(env_path: str) -> dict[str, str]:
    """
    .env ファイルを読み込んで {KEY: VALUE} dict を返す。
    ファイルが存在しない場合は空 dict を返す（エラーにしない）。
    """
    path = Path(env_path)
    if not path.exists():
        logger.debug(".env が見つかりません（%s）。環境変数のみ使用します。", env_path)
        return {}

    env: dict[str, str] = {}
    with path.open(encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            stripped = line.strip()
            # 空行・コメント行はスキップ
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                logger.debug(".env %d行目をスキップ（=が無い）: %s", line_num, stripped)
                continue
            key, _, value = stripped.partition("=")
            env[key.strip()] = value.strip()

    # システムの環境変数でさらに上書き（プロセス環境変数が最優先）
    for key in list(env.keys()):
        if key in os.environ:
            env[key] = os.environ[key]

    logger.debug(".env を読み込みました: keys=%s", list(env.keys()))
    return env


def _validate(
    yaml_cfg: dict,
    env_cfg: dict[str, str],
    llm_runtime_cfg: dict[str, Any],
) -> None:
    """
    設定値の妥当性を検証する。問題があれば例外を送出する。
    """
    # --- 必須セクション確認 ---
    required_sections = ("llm", "engine", "web_poster", "logging")
    for section in required_sections:
        if section not in yaml_cfg:
            raise ConfigValidationError(f"config.yaml に必須セクション '{section}' がありません。")

    llm_cfg = yaml_cfg["llm"]

    providers_cfg: dict = llm_cfg.get("providers", {})

    openai_section = providers_cfg.get("openai", {})
    if openai_section:
        api_mode = str(openai_section.get("api_mode", "auto"))
        if api_mode not in _VALID_OPENAI_API_MODES:
            raise ConfigValidationError(
                "providers.openai.api_mode は "
                f"{sorted(_VALID_OPENAI_API_MODES)} のいずれかである必要があります。"
            )

    profiles_raw = llm_runtime_cfg.get("profiles", {})
    if profiles_raw and not isinstance(profiles_raw, dict):
        raise ConfigValidationError(
            "llm_runtime.local.yaml の profiles はマッピング（dict）である必要があります。"
        )

    for profile_name, profile_raw in profiles_raw.items():
        if not isinstance(profile_raw, dict):
            raise ConfigValidationError(
                f"llm_runtime.local.yaml の profiles.{profile_name} は dict である必要があります。"
            )
        provider = str(profile_raw.get("provider", ""))
        if provider not in VALID_PROVIDERS:
            raise ConfigValidationError(
                f"llm_runtime.local.yaml の profiles.{profile_name}.provider '{provider}' は無効です。"
            )
        if provider not in providers_cfg:
            raise ConfigValidationError(
                f"llm_runtime.local.yaml の profiles.{profile_name}.provider '{provider}' に対応する "
                "providers 設定が config.yaml にありません。"
            )
        if not str(profile_raw.get("model", "")).strip():
            raise ConfigValidationError(
                f"llm_runtime.local.yaml の profiles.{profile_name}.model は必須です。"
            )

    active_profile = str(llm_runtime_cfg.get("active_profile", "")).strip()
    if active_profile and active_profile not in profiles_raw:
        raise ConfigValidationError(
            f"llm_runtime.local.yaml の active_profile '{active_profile}' は profiles に存在しません。"
        )

    story_overrides = llm_runtime_cfg.get("story_overrides", {})
    if story_overrides and not isinstance(story_overrides, dict):
        raise ConfigValidationError(
            "llm_runtime.local.yaml の story_overrides はマッピング（dict）である必要があります。"
        )
    for story_id, profile_name in story_overrides.items():
        if str(profile_name) not in profiles_raw:
            raise ConfigValidationError(
                f"llm_runtime.local.yaml の story_overrides.{story_id}='{profile_name}' は "
                "profiles に存在しません。"
            )

    logger.debug("設定バリデーション完了")


def _load_web_post_targets(web_post_targets_path: str) -> dict[str, Any]:
    """story ごとの Web 投稿設定ファイルを読み込む。"""
    path = Path(web_post_targets_path)
    if not path.exists():
        logger.debug(
            "web_post_targets.local.yaml が見つかりません（%s）。story 個別投稿設定は空のまま続行します。",
            web_post_targets_path,
        )
        return {}

    try:
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as exc:
        raise ConfigValidationError(
            f"web_post_targets.local.yaml の YAML 構文エラー: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise ConfigValidationError(
            "web_post_targets.local.yaml のトップレベルはマッピング（dict）である必要があります。"
        )
    return data


def _load_llm_runtime(llm_runtime_path: str) -> dict[str, Any]:
    """story ごとの runtime LLM 設定ファイルを読み込む。"""
    path = Path(llm_runtime_path)
    if not path.exists():
        logger.debug(
            "llm_runtime.local.yaml が見つかりません（%s）。runtime LLM 切替なしで続行します。",
            llm_runtime_path,
        )
        return {}

    try:
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as exc:
        raise ConfigValidationError(
            f"llm_runtime.local.yaml の YAML 構文エラー: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise ConfigValidationError(
            "llm_runtime.local.yaml のトップレベルはマッピング（dict）である必要があります。"
        )
    return data


def _build(
    yaml_cfg: dict,
    env_cfg: dict[str, str],
    web_post_targets_cfg: dict[str, Any],
    llm_runtime_cfg: dict[str, Any],
) -> Config:
    """
    yaml_cfg と env_cfg から Config dataclass を構築する。
    .env の値は config.yaml より優先する。
    """
    llm_raw = yaml_cfg["llm"]
    providers_raw: dict[str, Any] = llm_raw.get("providers", {})

    # --- プロバイダー設定を構築 ---
    built_providers: dict[str, Any] = {}

    ollama_raw = providers_raw.get("ollama", {})
    if ollama_raw:
        # .env の OLLAMA_BASE_URL が config.yaml の base_url を上書き
        base_url = env_cfg.get("OLLAMA_BASE_URL") or os.environ.get("OLLAMA_BASE_URL") or ollama_raw.get("base_url", "http://localhost:11434")
        built_providers["ollama"] = OllamaProviderConfig(
            base_url=base_url,
            default_model=ollama_raw.get("default_model", ""),
            timeout_sec=int(ollama_raw.get("timeout_sec", 120)),
            max_retries=int(ollama_raw.get("max_retries", 3)),
        )

    for provider_name in ("openai", "anthropic", "gemini", "deepseek"):
        raw = providers_raw.get(provider_name, {})
        if raw:
            api_key_env = _CLOUD_API_KEY_MAP[provider_name]
            built_providers[provider_name] = CloudProviderConfig(
                default_model=raw.get("default_model", ""),
                timeout_sec=int(raw.get("timeout_sec", 60)),
                max_retries=int(raw.get("max_retries", 3)),
                temperature=raw.get("temperature"),
                max_tokens=raw.get("max_tokens"),
                api_key=env_cfg.get(api_key_env, "") or os.environ.get(api_key_env, ""),
                api_mode=(
                    str(raw.get("api_mode", "auto"))
                    if provider_name == "openai"
                    else None
                ),
            )

    raw_profiles: dict[str, Any] = llm_raw.get("model_profiles", {})
    model_profiles: dict[str, dict[str, ModelProfileConfig]] = {}
    for provider_name, provider_profiles in raw_profiles.items():
        if not isinstance(provider_profiles, dict):
            continue
        built_profiles: dict[str, ModelProfileConfig] = {}
        for model_name, profile_raw in provider_profiles.items():
            if not isinstance(profile_raw, dict):
                continue
            built_profiles[str(model_name)] = ModelProfileConfig(
                reasoning_mode=str(profile_raw.get("reasoning_mode", "auto")),
                max_tokens=(
                    int(profile_raw["max_tokens"])
                    if profile_raw.get("max_tokens") is not None
                    else None
                ),
                recovery_max_tokens=(
                    int(profile_raw["recovery_max_tokens"])
                    if profile_raw.get("recovery_max_tokens") is not None
                    else None
                ),
                temperature=(
                    float(profile_raw["temperature"])
                    if profile_raw.get("temperature") is not None
                    else None
                ),
                recovery_temperature=(
                    float(profile_raw["recovery_temperature"])
                    if profile_raw.get("recovery_temperature") is not None
                    else None
                ),
                recovery_reasoning_mode=(
                    str(profile_raw["recovery_reasoning_mode"])
                    if profile_raw.get("recovery_reasoning_mode") is not None
                    else None
                ),
            )
        model_profiles[provider_name] = built_profiles

    llm_config = LLMConfig(
        cloud_concurrency=int(llm_raw.get("cloud_concurrency", 3)),
        providers=built_providers,
        model_profiles=model_profiles,
    )

    # --- エンジン設定 ---
    engine_raw = yaml_cfg["engine"]
    engine_config = EngineConfig(
        memory_limit=int(engine_raw.get("memory_limit", 20)),
        emotion_decay_rate=float(engine_raw.get("emotion_decay_rate", 0.05)),
        max_move_cost=int(engine_raw.get("max_move_cost", 1)),
    )

    # --- Web送信設定（batch/retry 系のみ） ---
    wp_raw = yaml_cfg["web_poster"]

    web_poster_config = WebPosterSettingsConfig(
        batch_size=int(wp_raw.get("batch_size", 10)),
        retry_interval_sec=int(wp_raw.get("retry_interval_sec", 30)),
        max_consecutive_failures=int(wp_raw.get("max_consecutive_failures", 3)),
        pause_duration_sec=int(wp_raw.get("pause_duration_sec", 300)),
    )

    built_web_post_targets: dict[str, StoryWebPostTargetConfig] = {}
    for story_id, raw in web_post_targets_cfg.items():
        if not isinstance(raw, dict):
            continue
        built_web_post_targets[str(story_id)] = StoryWebPostTargetConfig(
            enabled=bool(raw.get("enabled", True)),
            receiver_url=str(raw.get("receiver_url", "")),
            auth_token=str(raw.get("auth_token", "")),
        )

    runtime_profiles_raw = llm_runtime_cfg.get("profiles", {})
    built_runtime_profiles: dict[str, LLMRuntimeProfileConfig] = {}
    if isinstance(runtime_profiles_raw, dict):
        for profile_name, raw in runtime_profiles_raw.items():
            if not isinstance(raw, dict):
                continue
            built_runtime_profiles[str(profile_name)] = LLMRuntimeProfileConfig(
                provider=str(raw.get("provider", "")),
                model=str(raw.get("model", "")),
            )
    llm_runtime_config = LLMRuntimeConfig(
        active_profile=str(llm_runtime_cfg.get("active_profile", "")),
        profiles=built_runtime_profiles,
        story_overrides={
            str(story_id): str(profile_name)
            for story_id, profile_name in llm_runtime_cfg.get("story_overrides", {}).items()
        },
    )

    # --- ログ設定 ---
    log_raw = yaml_cfg["logging"]
    logging_config = LoggingConfig(
        level=log_raw.get("level", "INFO"),
        log_dir=log_raw.get("log_dir", "logs"),
        rotation=log_raw.get("rotation", "daily"),
        retention_days=int(log_raw.get("retention_days", 7)),
    )

    # --- ストーリーメモリ設定（v2 追加、セクション未定義時はデフォルト） ---
    sm_raw = yaml_cfg.get("story_memory", {})
    story_memory_config = StoryMemoryConfig(
        enabled=bool(sm_raw.get("enabled", False)),
        summarize_interval_turns=int(sm_raw.get("summarize_interval_turns", 10)),
        max_injection_count=int(sm_raw.get("max_injection_count", 5)),
        importance_threshold=float(sm_raw.get("importance_threshold", 0.3)),
        response_max_tokens=int(sm_raw.get("response_max_tokens", 220)),
        repair_max_tokens=int(sm_raw.get("repair_max_tokens", 320)),
    )

    # --- ナレーション設定（v2 追加、セクション未定義時はデフォルト） ---
    nr_raw = yaml_cfg.get("narration", {})
    narration_config = NarrationConfig(
        enabled=bool(nr_raw.get("enabled", False)),
        min_interval_turns=int(nr_raw.get("min_interval_turns", 3)),
        emotion_change_threshold=float(nr_raw.get("emotion_change_threshold", 0.3)),
        include_chapter_breaks=bool(nr_raw.get("include_chapter_breaks", True)),
        response_max_tokens=int(nr_raw.get("response_max_tokens", 480)),
        retry_max_tokens=int(nr_raw.get("retry_max_tokens", 360)),
        chapter_max_tokens=int(nr_raw.get("chapter_max_tokens", 120)),
    )

    # --- キャラクター動的進化設定（v2 Phase B 追加） ---
    ce_raw = yaml_cfg.get("character_evolution", {})
    character_evolution_config = CharacterEvolutionConfig(
        enabled=bool(ce_raw.get("enabled", False)),
        check_interval_rounds=int(ce_raw.get("check_interval_rounds", 1)),
        max_changes_per_check=int(ce_raw.get("max_changes_per_check", 2)),
    )

    # --- ストーリー演出家設定（v2 Phase B 追加） ---
    sd_raw = yaml_cfg.get("story_director", {})
    story_director_config = StoryDirectorConfig(
        enabled=bool(sd_raw.get("enabled", False)),
        analysis_interval_rounds=int(sd_raw.get("analysis_interval_rounds", 3)),
        max_active_interventions=int(sd_raw.get("max_active_interventions", 3)),
        max_active_tensions=int(sd_raw.get("max_active_tensions", 5)),
        tension_escalation_rounds=int(sd_raw.get("tension_escalation_rounds", 5)),
        stagnation_detection=bool(sd_raw.get("stagnation_detection", True)),
        intervention_strength=str(sd_raw.get("intervention_strength", "moderate")),
        min_intervention_duration_rounds=int(
            sd_raw.get("min_intervention_duration_rounds", 3)
        ),
        repeat_intervention_cooldown_rounds=int(
            sd_raw.get("repeat_intervention_cooldown_rounds", 4)
        ),
        max_same_scope_active_interventions=int(
            sd_raw.get("max_same_scope_active_interventions", 1)
        ),
    )

    # --- 小説自動生成設定（v2 Phase C 追加） ---
    ng_raw = yaml_cfg.get("novel_generator", {})
    novel_generator_config = NovelGeneratorConfig(
        enabled=bool(ng_raw.get("enabled", False)),
        generate_on_session_end=bool(ng_raw.get("generate_on_session_end", True)),
        generate_on_chapter_break=bool(ng_raw.get("generate_on_chapter_break", True)),
        response_max_tokens=int(ng_raw.get("response_max_tokens", 320)),
        repair_max_tokens=int(ng_raw.get("repair_max_tokens", 480)),
    )

    # --- 短期環境コンテキスト設定（v2 ambient 追加） ---
    ac_raw = yaml_cfg.get("ambient_context", {})
    ambient_context_config = AmbientContextConfig(
        enabled=bool(ac_raw.get("enabled", False)),
        min_duration_turns=int(ac_raw.get("min_duration_turns", 3)),
        max_duration_turns=int(ac_raw.get("max_duration_turns", 8)),
        max_active_body_factors=int(ac_raw.get("max_active_body_factors", 1)),
        max_active_place_factors=int(ac_raw.get("max_active_place_factors", 1)),
        max_prompt_items=int(ac_raw.get("max_prompt_items", 3)),
        llm_phrase_enabled=bool(ac_raw.get("llm_phrase_enabled", True)),
    )

    smgmt_raw = yaml_cfg.get("scene_management", {})
    scene_management_config = SceneManagementConfig(
        enabled=bool(smgmt_raw.get("enabled", False)),
        max_active_scenes_per_story=int(smgmt_raw.get("max_active_scenes_per_story", 3)),
    )

    pp_raw = yaml_cfg.get("participation_planner", {})
    participation_planner_config = ParticipationPlannerConfig(
        enabled=bool(pp_raw.get("enabled", False)),
        max_focus_characters=int(pp_raw.get("max_focus_characters", 2)),
        max_support_characters=int(pp_raw.get("max_support_characters", 2)),
        monologue_solo_rate=float(pp_raw.get("monologue_solo_rate", 0.0)),
        monologue_group_rate=float(pp_raw.get("monologue_group_rate", 0.0)),
        advance_monologue_bonus=float(pp_raw.get("advance_monologue_bonus", 0.0)),
        max_place_dialogue_lines=int(pp_raw.get("max_place_dialogue_lines", 30)),
    )

    sh_raw = yaml_cfg.get("story_hooks", {})
    story_hooks_config = StoryHooksConfig(
        enabled=bool(sh_raw.get("enabled", False)),
        max_open_hooks=int(sh_raw.get("max_open_hooks", 10)),
    )

    cd_raw = yaml_cfg.get("character_drives", {})
    character_drives_config = CharacterDrivesConfig(
        enabled=bool(cd_raw.get("enabled", False)),
        max_taboo_topics=int(cd_raw.get("max_taboo_topics", 3)),
    )

    rd_raw = yaml_cfg.get("relationship_dynamics", {})
    relationship_dynamics_config = RelationshipDynamicsConfig(
        enabled=bool(rd_raw.get("enabled", False)),
        trust_delta_scale=float(rd_raw.get("trust_delta_scale", 0.1)),
    )

    qg_raw = yaml_cfg.get("quality_guard", {})
    quality_guard_config = QualityGuardConfig(
        enabled=bool(qg_raw.get("enabled", False)),
        max_group_chars=int(qg_raw.get("max_group_chars", 180)),
        max_reply_chars=int(qg_raw.get("max_reply_chars", 140)),
        max_monologue_chars=int(qg_raw.get("max_monologue_chars", 110)),
        max_current_affairs_chars=int(qg_raw.get("max_current_affairs_chars", 180)),
        max_abstract_token_occurrences=int(qg_raw.get("max_abstract_token_occurrences", 3)),
        max_repeated_ngram_occurrences=int(qg_raw.get("max_repeated_ngram_occurrences", 2)),
        max_sentences=int(qg_raw.get("max_sentences", 2)),
    )

    ge_raw = yaml_cfg.get("growth_engine", {})
    growth_engine_config = GrowthEngineConfig(
        enabled=bool(ge_raw.get("enabled", False)),
        rolling_window_turns=int(ge_raw.get("rolling_window_turns", 24)),
        immediate_commit_identity_threshold=float(
            ge_raw.get("immediate_commit_identity_threshold", 0.80)
        ),
        goal_worry_aggregate_commit_threshold=float(
            ge_raw.get("goal_worry_aggregate_commit_threshold", 0.70)
        ),
        personality_core_aggregate_commit_threshold=float(
            ge_raw.get("personality_core_aggregate_commit_threshold", 1.30)
        ),
        personality_core_min_durable_support=int(
            ge_raw.get("personality_core_min_durable_support", 2)
        ),
        rich_evidence_min_items=int(ge_raw.get("rich_evidence_min_items", 3)),
        pending_candidate_ttl_turns=int(ge_raw.get("pending_candidate_ttl_turns", 24)),
        response_max_tokens=int(ge_raw.get("response_max_tokens", 260)),
        repair_max_tokens=int(ge_raw.get("repair_max_tokens", 360)),
    )

    ep_raw = yaml_cfg.get("episode_planner", {})
    episode_planner_config = EpisodePlannerConfig(
        enabled=bool(ep_raw.get("enabled", True)),
        min_turns_per_episode=int(ep_raw.get("min_turns_per_episode", 4)),
        max_turns_per_episode=int(ep_raw.get("max_turns_per_episode", 12)),
        carry_hook_limit=int(ep_raw.get("carry_hook_limit", 3)),
        stale_rounds_before_close=int(ep_raw.get("stale_rounds_before_close", 2)),
    )

    dp_raw = yaml_cfg.get("dramatic_pressure", {})
    dramatic_pressure_config = DramaticPressureConfig(
        enabled=bool(dp_raw.get("enabled", True)),
        recent_window_turns=int(dp_raw.get("recent_window_turns", 8)),
        min_pressure_score=float(dp_raw.get("min_pressure_score", 0.55)),
        stale_rounds_before_resolve=int(dp_raw.get("stale_rounds_before_resolve", 2)),
        max_active_pressures=int(dp_raw.get("max_active_pressures", 12)),
    )

    # --- Chapter 設定（v2 upgrade 追加） ---
    ch_raw = yaml_cfg.get("chapter", {})
    chapter_config = ChapterConfig(
        enabled=bool(ch_raw.get("enabled", False)),
        max_active_chapters=int(ch_raw.get("max_active_chapters", 1)),
        beat_check_interval_rounds=int(ch_raw.get("beat_check_interval_rounds", 1)),
        beat_timeout_turns=int(ch_raw.get("beat_timeout_turns", 30)),
        auto_event_injection=bool(ch_raw.get("auto_event_injection", True)),
        enable_llm_beat_judgment=bool(ch_raw.get("enable_llm_beat_judgment", False)),
        llm_beat_min_elapsed_turns=int(ch_raw.get("llm_beat_min_elapsed_turns", 5)),
    )

    # --- Director Persona 設定（v2 upgrade 追加） ---
    # dp_raw は dramatic_pressure で使用済みのため dp2_raw とする
    dp2_raw = yaml_cfg.get("director_persona", {})
    director_persona_config = DirectorPersonaConfig(
        enabled=bool(dp2_raw.get("enabled", False)),
        evaluation_interval_rounds=int(dp2_raw.get("evaluation_interval_rounds", 3)),
        steering_strength=str(dp2_raw.get("steering_strength", "moderate")),
        satisfaction_decay=float(dp2_raw.get("satisfaction_decay", 0.1)),
        allow_mid_chapter_swap=bool(dp2_raw.get("allow_mid_chapter_swap", True)),
        swap_cooldown_turns=int(dp2_raw.get("swap_cooldown_turns", 0)),
        preserve_satisfaction_on_swap=bool(dp2_raw.get("preserve_satisfaction_on_swap", True)),
        enable_llm_evaluation=bool(dp2_raw.get("enable_llm_evaluation", False)),
        llm_eval_interval_rounds=int(dp2_raw.get("llm_eval_interval_rounds", 3)),
    )

    # --- Chapter Generator 設定（Phase 4） ---
    cg_raw = yaml_cfg.get("chapter_generator", {})
    chapter_generator_config = ChapterGeneratorConfig(
        enabled=bool(cg_raw.get("enabled", False)),
        default_beat_timeout=int(cg_raw.get("default_beat_timeout", 20)),
        include_conflict_seeds=bool(cg_raw.get("include_conflict_seeds", True)),
        max_events_per_beat=int(cg_raw.get("max_events_per_beat", 2)),
    )

    # --- Pre-turn Scene Script 設定 ---
    ss_raw = yaml_cfg.get("scene_script", {})
    scene_script_config = SceneScriptConfig(
        enabled=bool(ss_raw.get("enabled", False)),
        generation_mode=str(ss_raw.get("generation_mode", "per_round")),
        format_mode=str(ss_raw.get("format_mode", "narrative")),
        injection_mode=str(ss_raw.get("injection_mode", "header")),
        max_sentences=int(ss_raw.get("max_sentences", 3)),
        reference_previous_n=int(ss_raw.get("reference_previous_n", 1)),
        response_max_tokens=int(ss_raw.get("response_max_tokens", 720)),
        repair_max_tokens=int(ss_raw.get("repair_max_tokens", 960)),
        max_directive_characters=int(ss_raw.get("max_directive_characters", 10)),
    )

    # --- 時事モード設定 ---
    nm_raw = yaml_cfg.get("news_mode", {})
    _default_presets = {
        "off":    NewsModeIntensityPreset(rate=0.0,  cooldown_turns=999),
        "low":    NewsModeIntensityPreset(rate=0.03, cooldown_turns=8),
        "medium": NewsModeIntensityPreset(rate=0.07, cooldown_turns=5),
        "high":   NewsModeIntensityPreset(rate=0.12, cooldown_turns=3),
        "rate20": NewsModeIntensityPreset(rate=0.20, cooldown_turns=3),
        "rate30": NewsModeIntensityPreset(rate=0.30, cooldown_turns=3),
        "rate40": NewsModeIntensityPreset(rate=0.40, cooldown_turns=3),
        "rate50": NewsModeIntensityPreset(rate=0.50, cooldown_turns=3),
    }
    _presets_raw = nm_raw.get("intensity_presets", {})
    built_presets: dict[str, NewsModeIntensityPreset] = dict(_default_presets)
    for preset_name, preset_raw in _presets_raw.items():
        if isinstance(preset_raw, dict):
            built_presets[str(preset_name)] = NewsModeIntensityPreset(
                rate=float(preset_raw.get("rate", 0.0)),
                cooldown_turns=int(preset_raw.get("cooldown_turns", 999)),
            )
    news_mode_config = NewsModeConfig(
        enabled=bool(nm_raw.get("enabled", False)),
        fetch_interval_sec=int(nm_raw.get("fetch_interval_sec", 1800)),
        http_timeout_sec=int(nm_raw.get("http_timeout_sec", 30)),
        user_agent=str(nm_raw.get("user_agent", "PocketRole/1.0 (+rss)")),
        allow_private_feed_hosts=bool(nm_raw.get("allow_private_feed_hosts", False)),
        max_feed_bytes=int(nm_raw.get("max_feed_bytes", 2_097_152)),
        max_entries_per_feed=int(nm_raw.get("max_entries_per_feed", 50)),
        max_articles_kept=int(nm_raw.get("max_articles_kept", 1000)),
        candidates_for_llm=int(nm_raw.get("candidates_for_llm", 8)),
        intensity_presets=built_presets,
    )

    return Config(
        llm=llm_config,
        llm_runtime=llm_runtime_config,
        engine=engine_config,
        web_poster=web_poster_config,
        web_post_targets=built_web_post_targets,
        logging=logging_config,
        story_memory=story_memory_config,
        narration=narration_config,
        character_evolution=character_evolution_config,
        story_director=story_director_config,
        novel_generator=novel_generator_config,
        ambient_context=ambient_context_config,
        scene_management=scene_management_config,
        participation_planner=participation_planner_config,
        story_hooks=story_hooks_config,
        character_drives=character_drives_config,
        relationship_dynamics=relationship_dynamics_config,
        quality_guard=quality_guard_config,
        growth_engine=growth_engine_config,
        episode_planner=episode_planner_config,
        dramatic_pressure=dramatic_pressure_config,
        chapter=chapter_config,
        director_persona=director_persona_config,
        chapter_generator=chapter_generator_config,
        scene_script=scene_script_config,
        news_mode=news_mode_config,
    )

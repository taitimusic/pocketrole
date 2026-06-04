"""
tests/test_config.py — engine/config.py のユニットテスト

戦略:
    tempfile / tmp_path fixture で一時ファイルを作成し、
    モックなしで実ファイル I/O をテストする。
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from engine.config import (
    AmbientContextConfig,
    APIKeyMissingError,
    CharacterDrivesConfig,
    CharacterEvolutionConfig,
    CloudProviderConfig,
    Config,
    ConfigFileNotFoundError,
    ConfigValidationError,
    EpisodePlannerConfig,
    GrowthEngineConfig,
    LLMRuntimeProfileConfig,
    LLMProviderNotConfiguredError,
    LLMRuntimeConfig,
    ModelProfileConfig,
    NarrationConfig,
    NewsModeConfig,
    OllamaProviderConfig,
    ParticipationPlannerConfig,
    QualityGuardConfig,
    RelationshipDynamicsConfig,
    SceneManagementConfig,
    StoryDirectorConfig,
    StoryHooksConfig,
    StoryMemoryConfig,
    StoryWebPostTargetConfig,
    WebPosterSettingsConfig,
    load_config,
)


# ============================================================
# ヘルパー
# ============================================================

MINIMAL_YAML = textwrap.dedent("""\
    llm:
      cloud_concurrency: 3
      providers:
        ollama:
          base_url: "http://localhost:11434"
          default_model: "qwen2.5:14b"
          timeout_sec: 120
          max_retries: 3
        openai:
          default_model: "gpt-4o-mini"
          api_mode: "auto"
          timeout_sec: 60
          max_retries: 3
          temperature: 0.8
        anthropic:
          default_model: "claude-sonnet-4-20250514"
          timeout_sec: 60
          max_retries: 3
          max_tokens: 300
        gemini:
          default_model: "gemini-2.0-flash"
          timeout_sec: 60
          max_retries: 3
        deepseek:
          default_model: "deepseek-chat"
          timeout_sec: 60
          max_retries: 3
    engine:
      memory_limit: 20
      emotion_decay_rate: 0.05
      max_move_cost: 1
    web_poster:
      batch_size: 10
      retry_interval_sec: 30
      max_consecutive_failures: 3
      pause_duration_sec: 300
    logging:
      level: "INFO"
      log_dir: "logs"
      rotation: "daily"
      retention_days: 7
""")

MINIMAL_ENV = textwrap.dedent("""\
    OLLAMA_BASE_URL=http://localhost:11434
""")

MINIMAL_TARGETS = textwrap.dedent("""\
    ankoku_gakuen:
      enabled: true
      receiver_url: "https://example.com/receiver.php"
      auth_token: "test_secret_token"
""")

MINIMAL_LLM_RUNTIME = textwrap.dedent("""\
    active_profile: "openai_nano"
    profiles:
      ollama_local:
        provider: "ollama"
        model: "ministral-3:14b"
      openai_nano:
        provider: "openai"
        model: "gpt-5.4-nano"
    story_overrides:
      ankoku_gakuen_mystery: "ollama_local"
""")

EXAMPLE_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml.example"


def write_files(
    tmp_path: Path,
    yaml_content: str = MINIMAL_YAML,
    env_content: str | None = MINIMAL_ENV,
    targets_content: str | None = MINIMAL_TARGETS,
    llm_runtime_content: str | None = None,
) -> tuple[str, str, str, str]:
    """一時ファイルに設定を書き込み、パスを返す。"""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml_content, encoding="utf-8")

    if env_content is not None:
        env_path = tmp_path / ".env"
        env_path.write_text(env_content, encoding="utf-8")
    else:
        env_path = tmp_path / ".env.missing"

    targets_path = tmp_path / "web_post_targets.local.yaml"
    if targets_content is not None:
        targets_path.write_text(targets_content, encoding="utf-8")

    llm_runtime_path = tmp_path / "llm_runtime.local.yaml"
    if llm_runtime_content is not None:
        llm_runtime_path.write_text(llm_runtime_content, encoding="utf-8")

    return str(config_path), str(env_path), str(targets_path), str(llm_runtime_path)


# ============================================================
# テストケース
# ============================================================

class TestLoadConfigSuccess:
    """正常系: config.yaml.example 相当の設定で正常読み込み。"""

    def test_load_config_success(self, tmp_path: Path) -> None:
        config_path, env_path, targets_path, llm_runtime_path = write_files(tmp_path)
        cfg = load_config(config_path, env_path, targets_path, llm_runtime_path)

        assert isinstance(cfg, Config)
        assert cfg.llm.cloud_concurrency == 3
        assert cfg.engine.memory_limit == 20
        assert cfg.web_poster.batch_size == 10
        assert cfg.web_post_targets["ankoku_gakuen"].receiver_url == "https://example.com/receiver.php"
        assert cfg.logging.level == "INFO"


    def test_news_mode_defaults_include_feed_safety_limits(self, tmp_path: Path) -> None:
        config_path, env_path, targets_path, llm_runtime_path = write_files(tmp_path)
        cfg = load_config(config_path, env_path, targets_path, llm_runtime_path)

        assert isinstance(cfg.news_mode, NewsModeConfig)
        assert cfg.news_mode.allow_private_feed_hosts is False
        assert cfg.news_mode.max_feed_bytes == 2_097_152
        assert cfg.news_mode.max_entries_per_feed == 50
        assert cfg.news_mode.intensity_presets["rate20"].rate == 0.20
        assert cfg.news_mode.intensity_presets["rate30"].rate == 0.30
        assert cfg.news_mode.intensity_presets["rate40"].rate == 0.40
        assert cfg.news_mode.intensity_presets["rate50"].rate == 0.50
        assert cfg.news_mode.intensity_presets["rate50"].cooldown_turns == 3


    def test_loads_news_mode_feed_safety_limits(self, tmp_path: Path) -> None:
        yaml_content = MINIMAL_YAML + textwrap.dedent("""\
            news_mode:
              enabled: true
              allow_private_feed_hosts: true
              max_feed_bytes: 4096
              max_entries_per_feed: 3
        """)
        config_path, env_path, targets_path, llm_runtime_path = write_files(
            tmp_path,
            yaml_content=yaml_content,
        )
        cfg = load_config(config_path, env_path, targets_path, llm_runtime_path)

        assert cfg.news_mode.allow_private_feed_hosts is True
        assert cfg.news_mode.max_feed_bytes == 4096
        assert cfg.news_mode.max_entries_per_feed == 3


    def test_loads_news_mode_high_frequency_presets(self, tmp_path: Path) -> None:
        yaml_content = MINIMAL_YAML + textwrap.dedent("""\
            news_mode:
              intensity_presets:
                rate20: { rate: 0.20, cooldown_turns: 3 }
                rate30: { rate: 0.30, cooldown_turns: 3 }
                rate40: { rate: 0.40, cooldown_turns: 3 }
                rate50: { rate: 0.50, cooldown_turns: 3 }
        """)
        config_path, env_path, targets_path, llm_runtime_path = write_files(
            tmp_path,
            yaml_content=yaml_content,
        )
        cfg = load_config(config_path, env_path, targets_path, llm_runtime_path)

        assert cfg.news_mode.get_preset("rate20").rate == 0.20
        assert cfg.news_mode.get_preset("rate30").rate == 0.30
        assert cfg.news_mode.get_preset("rate40").rate == 0.40
        assert cfg.news_mode.get_preset("rate50").rate == 0.50
        assert cfg.news_mode.get_preset("rate50").cooldown_turns == 3


class TestConfigNotFound:
    """config.yaml が存在しないパス → ConfigFileNotFoundError。"""

    def test_config_not_found(self, tmp_path: Path) -> None:
        missing = str(tmp_path / "nonexistent.yaml")
        with pytest.raises(ConfigFileNotFoundError):
            load_config(
                missing,
                str(tmp_path / ".env"),
                str(tmp_path / "targets.yaml"),
                str(tmp_path / "llm_runtime.local.yaml"),
            )


class TestEnvMissingOk:
    """.env が存在しない場合でも Ollama のみ使用時はエラーにならない。"""

    def test_env_missing_ok(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(MINIMAL_YAML, encoding="utf-8")
        missing_env = str(tmp_path / ".env")  # 存在しない

        # エラーなく Config が返る
        cfg = load_config(
            str(config_path),
            missing_env,
            str(tmp_path / "targets.yaml"),
            str(tmp_path / "llm_runtime.local.yaml"),
        )
        assert cfg.web_post_targets == {}


class TestInvalidYaml:
    """壊れた YAML → ConfigValidationError。"""

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text("llm: [\nbroken yaml: }{", encoding="utf-8")
        env_path = tmp_path / ".env"
        env_path.write_text("", encoding="utf-8")

        with pytest.raises(ConfigValidationError):
            load_config(
                str(config_path),
                str(env_path),
                str(tmp_path / "targets.yaml"),
                str(tmp_path / "llm_runtime.local.yaml"),
            )


class TestInvalidProvider:
    """llm_runtime.local.yaml の provider が未知の値 → ConfigValidationError。"""

    def test_invalid_provider(self, tmp_path: Path) -> None:
        llm_runtime_content = MINIMAL_LLM_RUNTIME.replace(
            'provider: "openai"', 'provider: "unknown_llm"', 1
        )
        config_path, env_path, targets_path, llm_runtime_path = write_files(
            tmp_path,
            llm_runtime_content=llm_runtime_content,
        )

        with pytest.raises(ConfigValidationError, match="unknown_llm"):
            load_config(config_path, env_path, targets_path, llm_runtime_path)


class TestRuntimeFileRequired:
    """runtime file は単一の切替点として必須。"""

    def test_runtime_file_missing_is_allowed_at_load_time(self, tmp_path: Path) -> None:
        config_path, env_path, targets_path, llm_runtime_path = write_files(
            tmp_path,
            llm_runtime_content=None,
        )

        cfg = load_config(config_path, env_path, targets_path, llm_runtime_path)
        assert cfg.llm_runtime.active_profile == ""


class TestAPIKeyMissing:
    """load_config は runtime 解決前なので APIキー未設定でも読める。"""

    def test_api_key_missing(self, tmp_path: Path) -> None:
        # .env には OPENAI_API_KEY を書かない
        env_content = "WEB_AUTH_TOKEN=token\n"
        config_path, env_path, targets_path, llm_runtime_path = write_files(
            tmp_path,
            env_content=env_content,
        )

        cfg = load_config(config_path, env_path, targets_path, llm_runtime_path)
        assert cfg.llm.providers["openai"].api_key == ""


class TestEnvOverridesBaseUrl:
    """OLLAMA_BASE_URL が config.yaml の base_url より優先される。"""

    def test_env_overrides_base_url(self, tmp_path: Path) -> None:
        env_content = MINIMAL_ENV + "OLLAMA_BASE_URL=http://custom-host:11434\n"
        config_path, env_path, targets_path, llm_runtime_path = write_files(
            tmp_path,
            MINIMAL_YAML,
            env_content,
        )

        cfg = load_config(config_path, env_path, targets_path, llm_runtime_path)
        ollama_cfg = cfg.llm.providers.get("ollama")
        assert isinstance(ollama_cfg, OllamaProviderConfig)
        assert ollama_cfg.base_url == "http://custom-host:11434"


class TestConfigTypes:
    """返り値が Config dataclass、各フィールドの型が正しいこと。"""

    def test_config_types(self, tmp_path: Path) -> None:
        config_path, env_path, targets_path, llm_runtime_path = write_files(tmp_path)
        cfg = load_config(config_path, env_path, targets_path, llm_runtime_path)

        # トップレベル
        assert isinstance(cfg, Config)

        # LLMConfig
        assert isinstance(cfg.llm.cloud_concurrency, int)
        assert isinstance(cfg.llm.providers, dict)

        # Ollama プロバイダー
        ollama = cfg.llm.providers.get("ollama")
        assert isinstance(ollama, OllamaProviderConfig)
        assert isinstance(ollama.base_url, str)
        assert isinstance(ollama.timeout_sec, int)
        assert isinstance(ollama.max_retries, int)

        # OpenAI プロバイダー
        openai_cfg = cfg.llm.providers.get("openai")
        assert isinstance(openai_cfg, CloudProviderConfig)
        assert isinstance(openai_cfg.temperature, float)
        assert openai_cfg.api_mode == "auto"

        # Anthropic プロバイダー
        anthropic_cfg = cfg.llm.providers.get("anthropic")
        assert isinstance(anthropic_cfg, CloudProviderConfig)
        assert isinstance(anthropic_cfg.max_tokens, int)
        assert anthropic_cfg.max_tokens == 300

        # EngineConfig
        assert isinstance(cfg.engine.memory_limit, int)
        assert isinstance(cfg.engine.emotion_decay_rate, float)
        assert isinstance(cfg.engine.max_move_cost, int)

        # WebPoster settings / targets
        assert isinstance(cfg.web_poster, WebPosterSettingsConfig)
        assert isinstance(cfg.web_poster.batch_size, int)
        assert isinstance(cfg.web_post_targets, dict)
        assert isinstance(cfg.web_post_targets["ankoku_gakuen"], StoryWebPostTargetConfig)
        assert isinstance(cfg.web_post_targets["ankoku_gakuen"].auth_token, str)

        # LoggingConfig
        assert isinstance(cfg.logging.level, str)
        assert isinstance(cfg.logging.retention_days, int)

        # StoryMemoryConfig / NarrationConfig
        assert isinstance(cfg.story_memory, StoryMemoryConfig)
        assert isinstance(cfg.narration, NarrationConfig)

        # CharacterEvolutionConfig / StoryDirectorConfig
        assert isinstance(cfg.character_evolution, CharacterEvolutionConfig)
        assert isinstance(cfg.story_director, StoryDirectorConfig)


class TestStoryScopedWebTargets:
    """Web 投稿先は story ごとの local yaml から読む。"""

    def test_story_scoped_targets_are_loaded(self, tmp_path: Path) -> None:
        config_path, env_path, targets_path, llm_runtime_path = write_files(tmp_path)

        cfg = load_config(config_path, env_path, targets_path, llm_runtime_path)

        assert cfg.web_post_targets["ankoku_gakuen"].enabled is True
        assert cfg.web_post_targets["ankoku_gakuen"].receiver_url == "https://example.com/receiver.php"
        assert cfg.web_post_targets["ankoku_gakuen"].auth_token == "test_secret_token"

    def test_env_web_receiver_variables_are_ignored(self, tmp_path: Path) -> None:
        env_content = textwrap.dedent("""\
            WEB_RECEIVER_URL=https://env-defined.example.com
            WEB_AUTH_TOKEN=env_token
        """)
        config_path, env_path, targets_path, llm_runtime_path = write_files(
            tmp_path,
            env_content=env_content,
        )

        cfg = load_config(config_path, env_path, targets_path, llm_runtime_path)

        assert cfg.web_post_targets["ankoku_gakuen"].receiver_url == "https://example.com/receiver.php"
        assert cfg.web_post_targets["ankoku_gakuen"].auth_token == "test_secret_token"

    def test_targets_file_missing_is_allowed_at_load_time(self, tmp_path: Path) -> None:
        config_path, env_path, targets_path, llm_runtime_path = write_files(
            tmp_path,
            targets_content=None,
        )

        cfg = load_config(config_path, env_path, targets_path, llm_runtime_path)

        assert cfg.web_post_targets == {}


class TestOpenAIApiMode:
    """OpenAI api_mode の読み込みとバリデーション。"""

    def test_openai_api_mode_is_loaded(self, tmp_path: Path) -> None:
        config_path, env_path, targets_path, llm_runtime_path = write_files(
            tmp_path,
            env_content=MINIMAL_ENV + "OPENAI_API_KEY=test-openai-key\n",
        )

        cfg = load_config(config_path, env_path, targets_path, llm_runtime_path)

        openai_cfg = cfg.llm.providers["openai"]
        assert isinstance(openai_cfg, CloudProviderConfig)
        assert openai_cfg.api_mode == "auto"

    def test_invalid_openai_api_mode_raises(self, tmp_path: Path) -> None:
        yaml_content = MINIMAL_YAML.replace('api_mode: "auto"', 'api_mode: "bogus"')
        config_path, env_path, targets_path, llm_runtime_path = write_files(
            tmp_path,
            yaml_content=yaml_content,
            env_content=MINIMAL_ENV + "OPENAI_API_KEY=test-openai-key\n",
        )

        with pytest.raises(ConfigValidationError, match="api_mode"):
            load_config(config_path, env_path, targets_path, llm_runtime_path)


class TestStoryMemoryConfigDefaults:
    """MINIMAL_YAML（新セクション無し）で story_memory / narration がデフォルト値になること。"""

    def test_story_memory_defaults(self, tmp_path: Path) -> None:
        config_path, env_path, targets_path, llm_runtime_path = write_files(tmp_path)
        cfg = load_config(config_path, env_path, targets_path, llm_runtime_path)

        assert cfg.story_memory.enabled is False
        assert cfg.story_memory.summarize_interval_turns == 10
        assert cfg.story_memory.max_injection_count == 5
        assert cfg.story_memory.importance_threshold == pytest.approx(0.3)

        assert cfg.narration.enabled is False
        assert cfg.narration.min_interval_turns == 3
        assert cfg.narration.emotion_change_threshold == pytest.approx(0.3)
        assert cfg.narration.include_chapter_breaks is True


class TestStoryMemoryConfigParsed:
    """story_memory + narration セクション付き YAML で正しくパースされること。"""

    def test_story_memory_parsed(self, tmp_path: Path) -> None:
        yaml_with_sections = MINIMAL_YAML + textwrap.dedent("""\
            story_memory:
              enabled: true
              summarize_interval_turns: 20
              max_injection_count: 8
              importance_threshold: 0.5
            narration:
              enabled: true
              min_interval_turns: 5
              emotion_change_threshold: 0.4
              include_chapter_breaks: false
              response_max_tokens: 510
              retry_max_tokens: 370
              chapter_max_tokens: 140
        """)
        config_path, env_path, targets_path, llm_runtime_path = write_files(
            tmp_path,
            yaml_with_sections,
        )
        cfg = load_config(config_path, env_path, targets_path, llm_runtime_path)

        assert cfg.story_memory.enabled is True
        assert cfg.story_memory.summarize_interval_turns == 20
        assert cfg.story_memory.max_injection_count == 8
        assert cfg.story_memory.importance_threshold == pytest.approx(0.5)

        assert cfg.narration.enabled is True
        assert cfg.narration.min_interval_turns == 5
        assert cfg.narration.emotion_change_threshold == pytest.approx(0.4)
        assert cfg.narration.include_chapter_breaks is False
        assert cfg.narration.response_max_tokens == 510
        assert cfg.narration.retry_max_tokens == 370
        assert cfg.narration.chapter_max_tokens == 140


class TestCharacterEvolutionConfigDefaults:
    """MINIMAL_YAML（新セクション無し）で character_evolution がデフォルト値になること。"""

    def test_character_evolution_defaults(self, tmp_path: Path) -> None:
        config_path, env_path, targets_path, llm_runtime_path = write_files(tmp_path)
        cfg = load_config(config_path, env_path, targets_path, llm_runtime_path)

        assert cfg.character_evolution.enabled is False
        assert cfg.character_evolution.check_interval_rounds == 1
        assert cfg.character_evolution.max_changes_per_check == 2


class TestCharacterEvolutionConfigParsed:
    """character_evolution セクション付き YAML で正しくパースされること。"""

    def test_character_evolution_parsed(self, tmp_path: Path) -> None:
        yaml_with_section = MINIMAL_YAML + textwrap.dedent("""\
            character_evolution:
              enabled: true
              check_interval_rounds: 2
              max_changes_per_check: 3
        """)
        config_path, env_path, targets_path, llm_runtime_path = write_files(
            tmp_path,
            yaml_with_section,
        )
        cfg = load_config(config_path, env_path, targets_path, llm_runtime_path)

        assert cfg.character_evolution.enabled is True
        assert cfg.character_evolution.check_interval_rounds == 2
        assert cfg.character_evolution.max_changes_per_check == 3


class TestStoryDirectorConfigDefaults:
    """MINIMAL_YAML（新セクション無し）で story_director がデフォルト値になること。"""

    def test_story_director_defaults(self, tmp_path: Path) -> None:
        config_path, env_path, targets_path, llm_runtime_path = write_files(tmp_path)
        cfg = load_config(config_path, env_path, targets_path, llm_runtime_path)

        assert cfg.story_director.enabled is False
        assert cfg.story_director.analysis_interval_rounds == 3
        assert cfg.story_director.max_active_interventions == 3
        assert cfg.story_director.max_active_tensions == 5
        assert cfg.story_director.tension_escalation_rounds == 5
        assert cfg.story_director.stagnation_detection is True
        assert cfg.story_director.intervention_strength == "moderate"
        assert cfg.story_director.min_intervention_duration_rounds == 3
        assert cfg.story_director.repeat_intervention_cooldown_rounds == 4
        assert cfg.story_director.max_same_scope_active_interventions == 1


class TestStoryDirectorConfigParsed:
    """story_director セクション付き YAML で正しくパースされること。"""

    def test_story_director_parsed(self, tmp_path: Path) -> None:
        yaml_with_section = MINIMAL_YAML + textwrap.dedent("""\
            story_director:
              enabled: true
              analysis_interval_rounds: 5
              max_active_interventions: 2
              max_active_tensions: 4
              tension_escalation_rounds: 8
              stagnation_detection: false
              intervention_strength: "dramatic"
              min_intervention_duration_rounds: 4
              repeat_intervention_cooldown_rounds: 6
              max_same_scope_active_interventions: 2
        """)
        config_path, env_path, targets_path, llm_runtime_path = write_files(
            tmp_path,
            yaml_with_section,
        )
        cfg = load_config(config_path, env_path, targets_path, llm_runtime_path)

        assert cfg.story_director.enabled is True
        assert cfg.story_director.analysis_interval_rounds == 5
        assert cfg.story_director.max_active_interventions == 2
        assert cfg.story_director.max_active_tensions == 4
        assert cfg.story_director.tension_escalation_rounds == 8
        assert cfg.story_director.stagnation_detection is False
        assert cfg.story_director.intervention_strength == "dramatic"
        assert cfg.story_director.min_intervention_duration_rounds == 4
        assert cfg.story_director.repeat_intervention_cooldown_rounds == 6
        assert cfg.story_director.max_same_scope_active_interventions == 2


class TestExampleConfigDefaults:
    """config.yaml.example がアップグレード機能有効の既定値を持つこと。"""

    def test_example_config_enables_upgrade_features(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        env_path = tmp_path / ".env"
        env_path.write_text("WEB_AUTH_TOKEN=test_secret_token\n", encoding="utf-8")

        cfg = load_config(
            str(config_path),
            str(env_path),
            str(tmp_path / "web_post_targets.local.yaml"),
            str(tmp_path / "llm_runtime.local.yaml"),
        )

        assert cfg.story_memory.enabled is True
        assert cfg.narration.enabled is True
        assert cfg.character_evolution.enabled is True
        assert cfg.story_director.enabled is True
        assert cfg.novel_generator.enabled is True
        assert cfg.ambient_context.enabled is False


class TestAmbientContextConfigDefaults:
    """MINIMAL_YAML（新セクション無し）で ambient_context がデフォルト値になること。"""

    def test_ambient_context_defaults(self, tmp_path: Path) -> None:
        config_path, env_path, targets_path, llm_runtime_path = write_files(tmp_path)
        cfg = load_config(config_path, env_path, targets_path, llm_runtime_path)

        assert isinstance(cfg.ambient_context, AmbientContextConfig)
        assert cfg.ambient_context.enabled is False
        assert cfg.ambient_context.min_duration_turns == 3
        assert cfg.ambient_context.max_duration_turns == 8
        assert cfg.ambient_context.max_active_body_factors == 1
        assert cfg.ambient_context.max_active_place_factors == 1
        assert cfg.ambient_context.max_prompt_items == 3
        assert cfg.ambient_context.llm_phrase_enabled is True


class TestAmbientContextConfigParsed:
    """ambient_context セクション付き YAML で正しくパースされること。"""

    def test_ambient_context_parsed(self, tmp_path: Path) -> None:
        yaml_with_section = MINIMAL_YAML + textwrap.dedent("""\
            ambient_context:
              enabled: true
              min_duration_turns: 4
              max_duration_turns: 6
              max_active_body_factors: 2
              max_active_place_factors: 2
              max_prompt_items: 4
              llm_phrase_enabled: false
        """)
        config_path, env_path, targets_path, llm_runtime_path = write_files(
            tmp_path,
            yaml_with_section,
        )
        cfg = load_config(config_path, env_path, targets_path, llm_runtime_path)

        assert cfg.ambient_context.enabled is True
        assert cfg.ambient_context.min_duration_turns == 4
        assert cfg.ambient_context.max_duration_turns == 6
        assert cfg.ambient_context.max_active_body_factors == 2
        assert cfg.ambient_context.max_active_place_factors == 2
        assert cfg.ambient_context.max_prompt_items == 4
        assert cfg.ambient_context.llm_phrase_enabled is False


class TestStoryEmergenceConfigDefaults:
    """MINIMAL_YAML で新しい story emergence 系設定がデフォルト値になること。"""

    def test_story_emergence_defaults(self, tmp_path: Path) -> None:
        config_path, env_path, targets_path, llm_runtime_path = write_files(tmp_path)
        cfg = load_config(config_path, env_path, targets_path, llm_runtime_path)

        assert isinstance(cfg.scene_management, SceneManagementConfig)
        assert isinstance(cfg.participation_planner, ParticipationPlannerConfig)
        assert isinstance(cfg.story_hooks, StoryHooksConfig)
        assert isinstance(cfg.character_drives, CharacterDrivesConfig)
        assert isinstance(cfg.relationship_dynamics, RelationshipDynamicsConfig)
        assert isinstance(cfg.quality_guard, QualityGuardConfig)
        assert isinstance(cfg.growth_engine, GrowthEngineConfig)

        assert cfg.scene_management.enabled is False
        assert cfg.participation_planner.enabled is False
        assert cfg.story_hooks.enabled is False
        assert cfg.character_drives.enabled is False
        assert cfg.relationship_dynamics.enabled is False
        assert cfg.quality_guard.enabled is False
        assert cfg.growth_engine.enabled is False
        assert cfg.growth_engine.rolling_window_turns == 24
        assert cfg.growth_engine.immediate_commit_identity_threshold == pytest.approx(0.80)
        assert cfg.growth_engine.goal_worry_aggregate_commit_threshold == pytest.approx(0.70)
        assert cfg.growth_engine.personality_core_aggregate_commit_threshold == pytest.approx(1.30)
        assert cfg.growth_engine.personality_core_min_durable_support == 2
        assert cfg.growth_engine.rich_evidence_min_items == 3
        assert cfg.growth_engine.pending_candidate_ttl_turns == 24
        assert cfg.growth_engine.response_max_tokens == 260
        assert cfg.growth_engine.repair_max_tokens == 360


class TestStoryEmergenceConfigParsed:
    """story emergence 系セクション付き YAML が正しくパースされること。"""

    def test_story_emergence_parsed(self, tmp_path: Path) -> None:
        yaml_with_sections = MINIMAL_YAML + textwrap.dedent("""\
            scene_management:
              enabled: true
              max_active_scenes_per_story: 4
            participation_planner:
              enabled: true
              max_focus_characters: 2
              max_support_characters: 2
            story_hooks:
              enabled: true
              max_open_hooks: 12
            story_memory:
              enabled: true
              summarize_interval_turns: 20
              response_max_tokens: 240
              repair_max_tokens: 360
            novel_generator:
              enabled: true
              generate_on_session_end: true
              generate_on_chapter_break: true
              response_max_tokens: 340
              repair_max_tokens: 500
            character_drives:
              enabled: true
              max_taboo_topics: 5
            relationship_dynamics:
              enabled: true
              trust_delta_scale: 0.2
            quality_guard:
              enabled: true
              max_group_chars: 170
              max_reply_chars: 130
              max_monologue_chars: 100
            growth_engine:
              enabled: true
              rolling_window_turns: 25
              immediate_commit_identity_threshold: 0.9
              goal_worry_aggregate_commit_threshold: 0.8
              personality_core_aggregate_commit_threshold: 1.4
              personality_core_min_durable_support: 3
              rich_evidence_min_items: 4
              pending_candidate_ttl_turns: 18
              response_max_tokens: 260
              repair_max_tokens: 380
            episode_planner:
              enabled: true
              min_turns_per_episode: 5
              max_turns_per_episode: 14
              carry_hook_limit: 4
              stale_rounds_before_close: 3
            scene_script:
              enabled: true
              generation_mode: per_turn
              response_max_tokens: 810
              repair_max_tokens: 990
              max_directive_characters: 8
        """)
        config_path, env_path, targets_path, llm_runtime_path = write_files(
            tmp_path,
            yaml_with_sections,
        )
        cfg = load_config(config_path, env_path, targets_path, llm_runtime_path)

        assert cfg.scene_management.enabled is True
        assert cfg.scene_management.max_active_scenes_per_story == 4
        assert cfg.participation_planner.enabled is True
        assert cfg.participation_planner.max_focus_characters == 2
        assert cfg.participation_planner.max_support_characters == 2
        assert cfg.story_hooks.enabled is True
        assert cfg.story_hooks.max_open_hooks == 12
        assert cfg.story_memory.response_max_tokens == 240
        assert cfg.story_memory.repair_max_tokens == 360
        assert cfg.novel_generator.response_max_tokens == 340
        assert cfg.novel_generator.repair_max_tokens == 500
        assert cfg.character_drives.enabled is True
        assert cfg.character_drives.max_taboo_topics == 5
        assert cfg.relationship_dynamics.enabled is True
        assert cfg.relationship_dynamics.trust_delta_scale == pytest.approx(0.2)
        assert cfg.quality_guard.enabled is True
        assert cfg.quality_guard.max_group_chars == 170
        assert cfg.quality_guard.max_reply_chars == 130
        assert cfg.quality_guard.max_monologue_chars == 100
        assert cfg.growth_engine.enabled is True
        assert cfg.growth_engine.rolling_window_turns == 25
        assert cfg.growth_engine.immediate_commit_identity_threshold == pytest.approx(0.9)
        assert cfg.growth_engine.goal_worry_aggregate_commit_threshold == pytest.approx(0.8)
        assert cfg.growth_engine.personality_core_aggregate_commit_threshold == pytest.approx(1.4)
        assert cfg.growth_engine.personality_core_min_durable_support == 3
        assert cfg.growth_engine.rich_evidence_min_items == 4
        assert cfg.growth_engine.pending_candidate_ttl_turns == 18
        assert cfg.growth_engine.response_max_tokens == 260
        assert cfg.growth_engine.repair_max_tokens == 380
        assert cfg.episode_planner.enabled is True
        assert cfg.episode_planner.min_turns_per_episode == 5
        assert cfg.scene_script.enabled is True
        assert cfg.scene_script.generation_mode == "per_turn"
        assert cfg.scene_script.response_max_tokens == 810
        assert cfg.scene_script.repair_max_tokens == 990
        assert cfg.scene_script.max_directive_characters == 8
        assert cfg.episode_planner.max_turns_per_episode == 14
        assert cfg.episode_planner.carry_hook_limit == 4
        assert cfg.episode_planner.stale_rounds_before_close == 3


class TestModelProfiles:
    """thinking 系モデル向け profile が読み込める。"""

    def test_load_model_profiles(self, tmp_path: Path) -> None:
        yaml_content = MINIMAL_YAML.replace(
            "  cloud_concurrency: 3\n",
            "  cloud_concurrency: 3\n\n"
            "  model_profiles:\n"
            "    ollama:\n"
            '      "qwen3.5:9b":\n'
            '        reasoning_mode: "auto"\n'
            "        max_tokens: 1000\n"
            "        recovery_max_tokens: 1400\n"
            '        recovery_reasoning_mode: "off"\n',
        )
        config_path, env_path, targets_path, llm_runtime_path = write_files(
            tmp_path,
            yaml_content,
        )

        cfg = load_config(config_path, env_path, targets_path, llm_runtime_path)

        profile = cfg.llm.model_profiles["ollama"]["qwen3.5:9b"]
        assert isinstance(profile, ModelProfileConfig)
        assert profile.reasoning_mode == "auto"
        assert profile.max_tokens == 1000
        assert profile.recovery_max_tokens == 1400
        assert profile.recovery_reasoning_mode == "off"


class TestLlmRuntimeConfig:
    def test_loads_llm_runtime_profiles(self, tmp_path: Path) -> None:
        config_path, env_path, targets_path, llm_runtime_path = write_files(
            tmp_path,
            llm_runtime_content=MINIMAL_LLM_RUNTIME,
        )

        cfg = load_config(
            config_path,
            env_path,
            targets_path,
            llm_runtime_path,
        )

        assert isinstance(cfg.llm_runtime, LLMRuntimeConfig)
        assert cfg.llm_runtime.active_profile == "openai_nano"
        assert cfg.llm_runtime.story_overrides["ankoku_gakuen_mystery"] == "ollama_local"
        assert isinstance(cfg.llm_runtime.profiles["openai_nano"], LLMRuntimeProfileConfig)
        assert cfg.llm_runtime.profiles["openai_nano"].provider == "openai"
        assert cfg.llm_runtime.profiles["openai_nano"].model == "gpt-5.4-nano"

    def test_invalid_llm_runtime_active_profile_raises(self, tmp_path: Path) -> None:
        config_path, env_path, targets_path, llm_runtime_path = write_files(
            tmp_path,
            llm_runtime_content=MINIMAL_LLM_RUNTIME.replace(
                'active_profile: "openai_nano"',
                'active_profile: "missing_profile"',
            ),
        )

        with pytest.raises(ConfigValidationError, match="active_profile"):
            load_config(
                config_path,
                env_path,
                targets_path,
                llm_runtime_path,
            )

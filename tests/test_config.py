"""Tests for provider configuration resolution from the environment."""

from __future__ import annotations

import pytest

from aitomation.config import ConfigError, LLMConfig

_ENV_VARS = [
    "AITOMATION_PROVIDER",
    "AITOMATION_MODEL",
    "AITOMATION_API_KEY",
    "AITOMATION_BASE_URL",
    "AITOMATION_TEMPERATURE",
    "AITOMATION_MAX_TOKENS",
    "AITOMATION_OUTPUT_MODE",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "DASHSCOPE_API_KEY",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_defaults_to_anthropic(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    cfg = LLMConfig.from_env()
    assert cfg.backend == "anthropic"
    assert cfg.model.startswith("claude")
    assert cfg.api_key == "sk-test"
    assert cfg.temperature == 0.0


def test_explicit_overrides_win_over_env(monkeypatch):
    monkeypatch.setenv("AITOMATION_PROVIDER", "anthropic")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    cfg = LLMConfig.from_env(backend="openai", model="gpt-foo")
    assert cfg.backend == "openai"
    assert cfg.model == "gpt-foo"
    assert cfg.api_key == "sk-openai"


def test_generic_api_key_takes_priority(monkeypatch):
    monkeypatch.setenv("AITOMATION_PROVIDER", "anthropic")
    monkeypatch.setenv("AITOMATION_API_KEY", "generic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "specific")
    cfg = LLMConfig.from_env()
    assert cfg.api_key == "generic"


def test_dashscope_gets_default_base_url(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "ds-key")
    cfg = LLMConfig.from_env(backend="dashscope")
    assert cfg.base_url and "dashscope" in cfg.base_url
    assert cfg.model.startswith("qwen")


def test_openai_compatible_requires_base_url(monkeypatch):
    monkeypatch.setenv("AITOMATION_API_KEY", "x")
    with pytest.raises(ConfigError, match="base_url"):
        LLMConfig.from_env(backend="openai-compatible")


def test_openai_compatible_ok_with_base_url(monkeypatch):
    monkeypatch.setenv("AITOMATION_BASE_URL", "http://localhost:11434/v1")
    cfg = LLMConfig.from_env(backend="openai-compatible")
    assert cfg.base_url == "http://localhost:11434/v1"
    # local servers may legitimately have no key
    assert cfg.api_key is None


def test_missing_key_raises(monkeypatch):
    with pytest.raises(ConfigError, match="API key"):
        LLMConfig.from_env(backend="anthropic")


def test_unknown_provider_raises():
    with pytest.raises(ConfigError, match="Unknown provider"):
        LLMConfig.from_env(backend="not-a-provider")


def test_dashscope_defaults_to_prompted_output(monkeypatch):
    # Qwen/DashScope rejects tool-call args carrying code blobs, so it must NOT use tool mode.
    monkeypatch.setenv("DASHSCOPE_API_KEY", "ds-key")
    cfg = LLMConfig.from_env(backend="dashscope")
    assert cfg.output_mode == "prompted"


def test_openai_compatible_defaults_to_prompted_output(monkeypatch):
    monkeypatch.setenv("AITOMATION_BASE_URL", "http://localhost:11434/v1")
    cfg = LLMConfig.from_env(backend="openai-compatible")
    assert cfg.output_mode == "prompted"


def test_anthropic_defaults_to_tool_output(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    cfg = LLMConfig.from_env(backend="anthropic")
    assert cfg.output_mode == "tool"


def test_output_mode_env_override_wins(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "ds-key")
    monkeypatch.setenv("AITOMATION_OUTPUT_MODE", "native")
    cfg = LLMConfig.from_env(backend="dashscope")
    assert cfg.output_mode == "native"


def test_invalid_output_mode_raises(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("AITOMATION_OUTPUT_MODE", "bogus")
    with pytest.raises(ConfigError, match="output mode"):
        LLMConfig.from_env(backend="anthropic")

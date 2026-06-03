"""Provider/model configuration.

BYO-key and model-agnostic by design: everything comes from env or explicit config,
nothing is hardcoded. One config object drives which backend the provider abstraction
talks to. Supported backends:

  anchored                env var               notes
  --------                -------               -----
  anthropic               ANTHROPIC_API_KEY     Claude models
  openai                  OPENAI_API_KEY        OpenAI models
  openai-compatible       AITOMATION_API_KEY    any OpenAI-compatible server (vLLM/Ollama/...)
  dashscope               DASHSCOPE_API_KEY     Alibaba Qwen via the OpenAI-compatible mode

`openai-compatible` and `dashscope` route through the OpenAI chat model with a custom
`base_url`, which is how Qwen/Ollama/vLLM are all covered with one adapter.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

Backend = Literal["anthropic", "openai", "openai-compatible", "dashscope"]

# How structured output is coerced from the model:
#   tool     — function/tool calling (Pydantic AI default; most reliable on Anthropic/OpenAI)
#   native   — provider-native JSON schema response_format
#   prompted — ask for JSON in the prompt and parse it from text (no tool calling)
# Qwen/DashScope and most local servers reject tool-call arguments that carry large code
# blobs ("function.arguments must be in JSON format"), so the OpenAI-compatible family
# defaults to `prompted`, which sidesteps server-side tool-argument validation entirely.
OutputMode = Literal["tool", "native", "prompted"]

# Sensible defaults per backend. Override with AITOMATION_MODEL. Kept conservative
# (balanced models) because discovery is analysis, not generation-heavy.
_DEFAULT_MODEL: dict[Backend, str] = {
    "anthropic": "claude-opus-4-8",
    "openai": "gpt-4.1",
    "openai-compatible": "qwen2.5-coder:7b",
    "dashscope": "qwen-plus",
}

# Structured-output mode per backend. Capable hosted models do tool calling reliably;
# Qwen/local OpenAI-compatible servers don't, so they default to prompted JSON.
_DEFAULT_OUTPUT_MODE: dict[Backend, OutputMode] = {
    "anthropic": "tool",
    "openai": "tool",
    "openai-compatible": "prompted",
    "dashscope": "prompted",
}

# API-key env var per backend, in priority order. AITOMATION_API_KEY always wins.
_KEY_ENV: dict[Backend, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "openai-compatible": (),
    "dashscope": ("DASHSCOPE_API_KEY",),
}

# Default base_url for backends that need one. dashscope uses the international
# (Singapore / ap-southeast-1) OpenAI-compatible endpoint the user prototypes against.
_DEFAULT_BASE_URL: dict[Backend, str | None] = {
    "anthropic": None,
    "openai": None,
    "openai-compatible": None,
    "dashscope": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
}


class ConfigError(RuntimeError):
    """Raised when provider configuration is incomplete (e.g. missing key/base_url)."""


@dataclass(slots=True)
class LLMConfig:
    backend: Backend
    model: str
    api_key: str | None = None
    base_url: str | None = None
    temperature: float = 0.0
    max_tokens: int = 8192
    output_mode: OutputMode = "tool"

    @classmethod
    def from_env(cls, *, backend: str | None = None, model: str | None = None) -> LLMConfig:
        """Build config from environment, with optional explicit overrides (e.g. CLI flags)."""
        chosen = (backend or os.getenv("AITOMATION_PROVIDER") or "anthropic").strip()
        if chosen not in _DEFAULT_MODEL:
            valid = ", ".join(_DEFAULT_MODEL)
            raise ConfigError(f"Unknown provider {chosen!r}. Choose one of: {valid}.")
        backend_t: Backend = chosen  # type: ignore[assignment]

        resolved_model = model or os.getenv("AITOMATION_MODEL") or _DEFAULT_MODEL[backend_t]

        api_key = os.getenv("AITOMATION_API_KEY")
        if not api_key:
            for env_name in _KEY_ENV[backend_t]:
                if value := os.getenv(env_name):
                    api_key = value
                    break

        base_url = os.getenv("AITOMATION_BASE_URL") or _DEFAULT_BASE_URL[backend_t]

        temperature = float(os.getenv("AITOMATION_TEMPERATURE", "0.0"))
        max_tokens = int(os.getenv("AITOMATION_MAX_TOKENS", "8192"))

        output_mode = (
            os.getenv("AITOMATION_OUTPUT_MODE") or _DEFAULT_OUTPUT_MODE[backend_t]
        ).strip()

        cfg = cls(
            backend=backend_t,
            model=resolved_model,
            api_key=api_key,
            base_url=base_url,
            temperature=temperature,
            max_tokens=max_tokens,
            output_mode=output_mode,  # type: ignore[arg-type]
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        """Surface misconfiguration early, with an actionable message."""
        if self.output_mode not in ("tool", "native", "prompted"):
            raise ConfigError(
                f"Unknown output mode {self.output_mode!r}. Set AITOMATION_OUTPUT_MODE to "
                "one of: tool, native, prompted."
            )
        if self.backend == "openai-compatible" and not self.base_url:
            raise ConfigError(
                "openai-compatible provider needs a base_url. Set AITOMATION_BASE_URL "
                "(e.g. http://localhost:11434/v1 for Ollama)."
            )
        # Local servers often need no key; remote backends do.
        needs_key = self.backend in ("anthropic", "openai", "dashscope")
        if needs_key and not self.api_key:
            envs = " or ".join(("AITOMATION_API_KEY", *_KEY_ENV[self.backend]))
            raise ConfigError(f"No API key for provider {self.backend!r}. Set {envs}.")

"""LLM provider abstraction.

The single internal interface every stage talks to. `generate_structured` is the load-
bearing one: discovery depends on getting back a validated Pydantic model regardless of
which backend (Anthropic, OpenAI, OpenAI-compatible/local) produced it. We lean on
Pydantic AI for typed structured output and normalize provider construction here so the
rest of the toolkit never imports a backend SDK directly.

AI is the analyst and author here — never the judge. Nothing in this layer decides
pass/fail; it only produces inventories and drafts for human review.
"""

from __future__ import annotations

import time
from typing import Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel
from pydantic_ai import Agent, NativeOutput, PromptedOutput
from pydantic_ai.models import Model
from pydantic_ai.output import OutputSpec
from pydantic_ai.settings import ModelSettings

from .config import LLMConfig
from .telemetry import UsageRecorder, _now

T = TypeVar("T", bound=BaseModel)


@runtime_checkable
class LLMProvider(Protocol):
    """What every stage of the toolkit depends on. Backend-agnostic by contract.

    `label` names the operation/prompt for usage instrumentation (e.g. 'discover.openapi',
    'write:test_pet_lifecycle')."""

    async def generate(self, prompt: str, *, system: str | None = None, label: str = "generate") -> str: ...

    async def generate_structured(
        self, prompt: str, schema: type[T], *, system: str | None = None, label: str = "generate_structured"
    ) -> T: ...


def _wrap_output(schema: type[T], mode: str) -> OutputSpec[T]:
    """Coerce the requested structured-output mode into a Pydantic AI output spec.

    `tool` is the default (function calling). `prompted`/`native` exist because Qwen and
    local OpenAI-compatible servers reject tool-call arguments carrying large code blobs;
    prompted output asks for JSON in the prompt and parses the text, sidestepping the
    provider's server-side tool-argument validation."""
    if mode == "prompted":
        return PromptedOutput(schema)
    if mode == "native":
        return NativeOutput(schema)
    return schema  # "tool" — Pydantic AI's default tool-calling output


def _build_settings(cfg: LLMConfig) -> ModelSettings:
    """Model settings for the backend.

    On Anthropic we opt into prompt caching of the stable prefix — the system prompt and the
    tool/output-schema definitions. Those are identical across every call in a run (a static
    system prompt + a fixed Pydantic schema), so caching them bills the prefix at ~0.1x after
    the first call. OpenAI/DashScope and local servers do prefix caching implicitly server-
    side, so they need no equivalent flag here."""
    base = dict(temperature=cfg.temperature, max_tokens=cfg.max_tokens)
    if cfg.backend == "anthropic":
        from pydantic_ai.models.anthropic import AnthropicModelSettings

        return AnthropicModelSettings(
            **base,
            anthropic_cache_instructions=True,
            anthropic_cache_tool_definitions=True,
        )
    return ModelSettings(**base)


def _build_model(cfg: LLMConfig) -> Model:
    """Construct the concrete Pydantic AI model for the configured backend."""
    if cfg.backend == "anthropic":
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider

        provider = AnthropicProvider(api_key=cfg.api_key, base_url=cfg.base_url)
        return AnthropicModel(cfg.model, provider=provider)

    # openai, openai-compatible, dashscope all speak the OpenAI chat protocol; the only
    # difference is base_url, which is exactly how local servers (Ollama/vLLM) and Qwen
    # are covered with one adapter.
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    provider = OpenAIProvider(base_url=cfg.base_url, api_key=cfg.api_key)
    return OpenAIChatModel(cfg.model, provider=provider)


class PydanticAIProvider:
    """`LLMProvider` backed by Pydantic AI. Constructs the model once; spins up a cheap
    per-call `Agent` so each request can carry its own output type and system prompt."""

    def __init__(self, config: LLMConfig, recorder: UsageRecorder | None = None) -> None:
        self.config = config
        self.recorder = recorder
        self._model = _build_model(config)
        self._settings = _build_settings(config)

    @classmethod
    def from_env(cls, *, backend: str | None = None, model: str | None = None) -> "PydanticAIProvider":
        return cls(LLMConfig.from_env(backend=backend, model=model))

    async def _run(self, agent: Agent, prompt: str, system: str | None, label: str):
        """Run an agent and, if a recorder is attached, capture usage for this call."""
        if self.recorder is None:
            return await agent.run(prompt)

        started_at, t0 = _now(), time.perf_counter()
        result = None
        ok, error = True, None
        try:
            result = await agent.run(prompt)
            return result
        except Exception as e:  # noqa: BLE001 — record the failed call, then re-raise
            ok, error = False, f"{type(e).__name__}: {e}"
            raise
        finally:
            self.recorder.record(
                label=label,
                provider=self.config.backend,
                model=self.config.model,
                system=system,
                user=prompt,
                usage=getattr(result, "usage", None),
                duration_s=time.perf_counter() - t0,
                started_at=started_at,
                ended_at=_now(),
                ok=ok,
                error=error,
            )

    async def generate(self, prompt: str, *, system: str | None = None, label: str = "generate") -> str:
        agent: Agent[None, str] = Agent(
            self._model,
            system_prompt=system or (),
            model_settings=self._settings,
        )
        result = await self._run(agent, prompt, system, label)
        return result.output

    async def generate_structured(
        self, prompt: str, schema: type[T], *, system: str | None = None, label: str = "generate_structured"
    ) -> T:
        agent: Agent[None, T] = Agent(
            self._model,
            output_type=_wrap_output(schema, self.config.output_mode),
            system_prompt=system or (),
            model_settings=self._settings,
        )
        result = await self._run(agent, prompt, system, label)
        return result.output

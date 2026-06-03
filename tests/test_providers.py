"""Tests for the provider abstraction's structured-output mode wrapping.

The mapping is load-bearing: Qwen/local OpenAI-compatible servers reject tool-call
arguments carrying code blobs, so those backends must NOT use tool-calling output."""

from __future__ import annotations

from pydantic import BaseModel
from pydantic_ai import NativeOutput, PromptedOutput

from aitomation.config import LLMConfig
from aitomation.providers import _wrap_output, list_models


class _Draft(BaseModel):
    code: str


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_list_models_openai_shape_dedupes_and_sorts(monkeypatch):
    captured: dict = {}

    def fake_get(url, headers=None, **kwargs):
        captured["url"], captured["headers"] = url, headers
        return _Resp({"data": [{"id": "qwen-plus"}, {"id": "qwen-max"}, {"id": "qwen-plus"}]})

    monkeypatch.setattr("httpx.get", fake_get)
    cfg = LLMConfig(
        backend="dashscope",
        model="qwen-plus",
        api_key="k",
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    )
    assert list_models(cfg) == ["qwen-max", "qwen-plus"]  # deduped + sorted
    assert captured["url"].endswith("/compatible-mode/v1/models")
    assert captured["headers"]["Authorization"] == "Bearer k"


def test_list_models_anthropic_uses_its_headers(monkeypatch):
    captured: dict = {}

    def fake_get(url, headers=None, **kwargs):
        captured["url"], captured["headers"] = url, headers
        return _Resp({"data": [{"id": "claude-opus-4-8"}, {"id": "claude-sonnet-4-6"}]})

    monkeypatch.setattr("httpx.get", fake_get)
    cfg = LLMConfig(backend="anthropic", model="claude-opus-4-8", api_key="ak")
    assert list_models(cfg) == ["claude-opus-4-8", "claude-sonnet-4-6"]
    assert captured["url"] == "https://api.anthropic.com/v1/models"
    assert captured["headers"]["x-api-key"] == "ak"
    assert captured["headers"]["anthropic-version"]


def test_wrap_output_tool_passes_schema_through():
    # tool mode is Pydantic AI's default: hand the bare schema to the Agent.
    assert _wrap_output(_Draft, "tool") is _Draft


def test_wrap_output_prompted_wraps_in_prompted_output():
    wrapped = _wrap_output(_Draft, "prompted")
    assert isinstance(wrapped, PromptedOutput)


def test_wrap_output_native_wraps_in_native_output():
    wrapped = _wrap_output(_Draft, "native")
    assert isinstance(wrapped, NativeOutput)

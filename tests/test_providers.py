"""Tests for the provider abstraction's structured-output mode wrapping.

The mapping is load-bearing: Qwen/local OpenAI-compatible servers reject tool-call
arguments carrying code blobs, so those backends must NOT use tool-calling output."""

from __future__ import annotations

from pydantic import BaseModel
from pydantic_ai import NativeOutput, PromptedOutput

from aitomation.providers import _wrap_output


class _Draft(BaseModel):
    code: str


def test_wrap_output_tool_passes_schema_through():
    # tool mode is Pydantic AI's default: hand the bare schema to the Agent.
    assert _wrap_output(_Draft, "tool") is _Draft


def test_wrap_output_prompted_wraps_in_prompted_output():
    wrapped = _wrap_output(_Draft, "prompted")
    assert isinstance(wrapped, PromptedOutput)


def test_wrap_output_native_wraps_in_native_output():
    wrapped = _wrap_output(_Draft, "native")
    assert isinstance(wrapped, NativeOutput)

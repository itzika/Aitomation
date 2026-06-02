"""Guard the bundled example specs against rot. Pure parsing — no LLM involved."""

from __future__ import annotations

from pathlib import Path

import pytest

from aitomation.discover.openapi import load_spec, summarize_spec

EXAMPLES = Path(__file__).parent.parent / "examples"


@pytest.mark.parametrize(
    "filename, op_count",
    [
        ("petstore-mini.json", 5),
        ("rickandmorty.openapi.json", 6),
    ],
)
def test_example_spec_summarizes(filename, op_count):
    summary = summarize_spec(load_spec(str(EXAMPLES / filename)))
    assert len(summary.operations) == op_count
    assert summary.base_url.startswith("http")


def test_rickandmorty_is_read_only_and_unauthenticated():
    summary = summarize_spec(load_spec(str(EXAMPLES / "rickandmorty.openapi.json")))
    assert summary.security_schemes == {}  # public, no auth
    assert all(op.method == "GET" for op in summary.operations)
    assert all(not op.security for op in summary.operations)

"""Tests for deterministic AsyncAPI extraction and discovery orchestration.

Like test_openapi, these cover the parts that must be reliable without an LLM: parsing
2.x and 3.x specs, resolving/inlining message payload `$ref`s, and that discover_asyncapi
backfills ground truth while the judgement call (stubbed) only supplies priorities/journeys.
"""

from __future__ import annotations

from pathlib import Path
from typing import TypeVar

import pytest
from pydantic import BaseModel

from aitomation.discover.asyncapi import (
    discover_asyncapi,
    elements_from_async_summary,
    summarize_asyncapi,
)
from aitomation.discover.openapi import load_spec
from aitomation.models import CoverageInventory

SPEC = Path(__file__).parent.parent / "examples" / "asyncapi.yaml"

T = TypeVar("T", bound=BaseModel)


@pytest.fixture
def summary():
    return summarize_asyncapi(load_spec(str(SPEC)))


def test_v3_channels_and_operations(summary):
    assert summary.asyncapi_version.startswith("3")
    by_name = {c.name: c for c in summary.channels}
    assert set(by_name) == {"orderCreated", "orderShipped"}
    assert by_name["orderCreated"].address == "orders.created"
    # 3.x operations carry the action verb; attributed back onto the channel.
    assert by_name["orderCreated"].operations == ["receive"]
    assert by_name["orderShipped"].operations == ["send"]


def test_v3_messages_collected_once_with_payload(summary):
    assert set(summary.messages) == {"OrderCreated", "OrderShipped"}
    created = summary.messages["OrderCreated"]
    # payload $ref to components/schemas/Order resolved and inlined
    assert created.payload["type"] == "object"
    assert "orderId" in created.payload["properties"]
    # nested $ref (items -> LineItem) is inlined too, so the schema is self-contained
    items = created.payload["properties"]["items"]
    assert items["type"] == "array"
    assert "sku" in items["items"]["properties"]


def test_elements_topics_and_event_schemas(summary):
    elements = elements_from_async_summary(summary)
    topics = {e.name: e for e in elements if e.kind == "topic"}
    schemas = {e.name: e for e in elements if e.kind == "event_schema"}
    assert set(topics) == {"orderCreated", "orderShipped"}
    assert topics["orderShipped"].method == "send"
    assert topics["orderShipped"].location == "orders.shipped"

    assert set(schemas) == {"OrderCreated", "OrderShipped"}
    created = schemas["OrderCreated"]
    # top-level payload fields become message inputs; required preserved
    by_field = {i.name: i for i in created.inputs}
    assert by_field["orderId"].required is True and by_field["orderId"].where == "message"
    assert by_field["currency"].required is False
    # the resolved JSON Schema rides along for the contract test
    assert created.json_schema and "orderId" in created.json_schema["properties"]


def test_v2_publish_subscribe_extraction():
    spec = {
        "asyncapi": "2.6.0",
        "info": {"title": "User Events", "version": "1"},
        "channels": {
            "user/signedup": {
                "description": "A user registered.",
                "subscribe": {
                    "message": {
                        "name": "UserSignedUp",
                        "payload": {
                            "type": "object",
                            "required": ["id"],
                            "properties": {"id": {"type": "string"}, "email": {"type": "string"}},
                        },
                    }
                },
            }
        },
    }
    summary = summarize_asyncapi(spec)
    ch = summary.channels[0]
    assert ch.name == "user/signedup" and ch.operations == ["subscribe"]
    assert "UserSignedUp" in summary.messages
    elements = elements_from_async_summary(summary)
    schema = next(e for e in elements if e.kind == "event_schema")
    assert schema.name == "UserSignedUp"
    assert {i.name for i in schema.inputs} == {"id", "email"}


def test_message_reused_across_channels_recorded_once():
    spec = {
        "asyncapi": "2.6.0",
        "info": {"title": "X", "version": "1"},
        "components": {
            "messages": {"Ping": {"name": "Ping", "payload": {"type": "object"}}}
        },
        "channels": {
            "a": {"publish": {"message": {"$ref": "#/components/messages/Ping"}}},
            "b": {"subscribe": {"message": {"$ref": "#/components/messages/Ping"}}},
        },
    }
    summary = summarize_asyncapi(spec)
    assert list(summary.messages) == ["Ping"]  # collected once
    assert summary.messages["Ping"].channels == ["a", "b"]  # both channels noted


class _FakeJudge:
    """Stands in for the LLM judgement layer — priorities/journeys only, never the surface."""

    def __init__(self) -> None:
        self.last_prompt: str | None = None
        self.last_system: str | None = None

    async def generate(self, prompt, *, system=None, label=""):  # pragma: no cover
        return ""

    async def generate_structured(self, prompt, schema: type[T], *, system=None, label: str = "") -> T:
        self.last_prompt = prompt
        self.last_system = system
        return schema(
            system_summary="Order domain events.",
            auth_strategy=None,
            high_priority=["OrderCreated"],
            low_priority=["ghost"],
            suggested_journeys=[
                {"name": "OrderCreated conforms", "description": "validate payload",
                 "priority": "high", "steps": [], "elements": ["OrderCreated", "ghost"]},
            ],
        )


async def test_discover_asyncapi_enumerates_deterministically():
    provider = _FakeJudge()
    inv = await discover_asyncapi(str(SPEC), provider)

    assert isinstance(inv, CoverageInventory)
    assert inv.source == "asyncapi"
    assert inv.system_name == "Orders Events"
    assert inv.summary == "Order domain events."

    kinds = inv.counts_by_kind()
    assert kinds.get("topic") == 2 and kinds.get("event_schema") == 2

    # priority from judgement applied; the rest default to medium
    assert next(e for e in inv.elements if e.name == "OrderCreated").priority == "high"
    # journey refs filtered to real names ('ghost' dropped)
    assert "ghost" not in inv.suggested_journeys[0].elements
    # judgement was steered by the authoritative element list + an event-specific system prompt
    assert "OrderCreated" in (provider.last_prompt or "")
    assert provider.last_system and "event-driven" in provider.last_system.lower()


async def test_discover_asyncapi_rejects_empty_spec(tmp_path):
    empty = tmp_path / "empty.yaml"
    empty.write_text("asyncapi: 3.0.0\ninfo:\n  title: x\n  version: '1'\nchannels: {}\n")
    with pytest.raises(ValueError):
        await discover_asyncapi(str(empty), _FakeJudge())

"""Tests for schema-registry discovery. The HTTP layer is mocked with httpx.MockTransport,
so these exercise the real parsing/element-building/orchestration without a live registry."""

from __future__ import annotations

import json
from typing import TypeVar

import httpx
import pytest
from pydantic import BaseModel

from aitomation.discover.registry import (
    discover_registry,
    elements_from_registry,
    fetch_registry,
)
from aitomation.models import CoverageInventory

T = TypeVar("T", bound=BaseModel)

BASE = "http://registry:8081"

_LATEST = {
    "orders.created-value": {
        "version": 1,
        "schemaType": "JSON",
        "schema": json.dumps(
            {
                "type": "object",
                "required": ["orderId"],
                "properties": {"orderId": {"type": "string"}, "amount": {"type": "number"}},
            }
        ),
    },
    "users-value": {
        "version": 2,
        # schemaType omitted on purpose — the registry defaults to AVRO
        "schema": json.dumps(
            {
                "type": "record",
                "name": "User",
                "fields": [
                    {"name": "id", "type": "string"},
                    {"name": "email", "type": ["null", "string"]},
                ],
            }
        ),
    },
    "raw-subject": {"version": 1, "schemaType": "PROTOBUF", "schema": 'syntax = "proto3";'},
}


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/subjects":
        return httpx.Response(200, json=list(_LATEST))
    for name, payload in _LATEST.items():
        if path == f"/subjects/{name}/versions/latest":
            return httpx.Response(200, json=payload)
    return httpx.Response(404)


def _client() -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(_handler), base_url=BASE)


def test_fetch_registry_reads_subjects_and_types():
    summary = fetch_registry(BASE, client=_client())
    by = {s.subject: s for s in summary.subjects}
    assert set(by) == set(_LATEST)
    assert by["orders.created-value"].schema_type == "JSON"
    assert by["users-value"].schema_type == "AVRO"  # defaulted
    assert by["raw-subject"].schema_type == "PROTOBUF"
    assert by["orders.created-value"].parsed["type"] == "object"


def test_elements_topics_derived_and_schemas_mapped():
    summary = fetch_registry(BASE, client=_client())
    elements = elements_from_registry(summary)
    topics = {e.location for e in elements if e.kind == "topic"}
    # -value subjects yield topics; the suffix-less raw-subject does not
    assert topics == {"orders.created", "users"}

    schemas = {e.name: e for e in elements if e.kind == "event_schema"}
    # JSON subject carries a validatable schema + typed fields
    oc = schemas["orders.created-value"]
    assert oc.json_schema and "orderId" in oc.json_schema["properties"]
    by_field = {i.name: i for i in oc.inputs}
    assert by_field["orderId"].required is True and by_field["amount"].required is False

    # Avro subject: nullable-union field is optional, no JSON Schema attached
    users = schemas["users-value"]
    assert users.json_schema is None
    uf = {i.name: i for i in users.inputs}
    assert uf["id"].required is True and uf["email"].required is False

    # Protobuf subject recorded as present, no fields/schema to validate
    raw = schemas["raw-subject"]
    assert raw.json_schema is None and raw.inputs == []


class _FakeJudge:
    def __init__(self) -> None:
        self.last_system: str | None = None

    async def generate(self, prompt, *, system=None, label=""):  # pragma: no cover
        return ""

    async def generate_structured(
        self, prompt, schema: type[T], *, system=None, label: str = ""
    ) -> T:
        self.last_system = system
        return schema(
            system_summary="Registry of order/user events.",
            auth_strategy=None,
            high_priority=["orders.created-value"],
            low_priority=[],
            suggested_journeys=[],
        )


async def test_discover_registry_end_to_end():
    provider = _FakeJudge()
    inv = await discover_registry(BASE, provider, client=_client())
    assert isinstance(inv, CoverageInventory)
    assert inv.source == "schema_registry"
    assert inv.counts_by_kind().get("event_schema") == 3
    assert next(e for e in inv.elements if e.name == "orders.created-value").priority == "high"
    assert provider.last_system and "schema registry" in provider.last_system.lower()


async def test_discover_registry_rejects_empty():
    def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    client = httpx.Client(transport=httpx.MockTransport(empty), base_url=BASE)
    with pytest.raises(ValueError):
        await discover_registry(BASE, _FakeJudge(), client=client)

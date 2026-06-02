"""Tests for the deterministic OpenAPI extraction and the discovery orchestration.

These cover the parts that must be reliable without an LLM in the loop: spec parsing,
the compact summary, and that discover_openapi backfills ground-truth fields. The LLM
call itself is stubbed with a fake provider — we test the seam, not the model.
"""

from __future__ import annotations

from pathlib import Path
from typing import TypeVar

import pytest
from pydantic import BaseModel

from aitomation.discover.openapi import (
    _auth_schemes_from_spec,
    build_judgment_prompt,
    discover_openapi,
    elements_from_summary,
    load_spec,
    render_summary,
    summarize_spec,
)
from aitomation.models import CoverageInventory

SPEC = Path(__file__).parent.parent / "examples" / "petstore-mini.json"

T = TypeVar("T", bound=BaseModel)


@pytest.fixture
def summary():
    return summarize_spec(load_spec(str(SPEC)))


def test_load_spec_reads_json(summary):
    assert summary.title == "Petstore Mini"
    assert summary.base_url == "https://api.petstore.example/v1"
    assert "bearerAuth" in summary.security_schemes


def test_summarize_enumerates_all_operations(summary):
    ops = {(o.method, o.path) for o in summary.operations}
    assert ops == {
        ("GET", "/pets"),
        ("POST", "/pets"),
        ("GET", "/pets/{petId}"),
        ("DELETE", "/pets/{petId}"),
        ("POST", "/login"),
    }


def test_path_level_params_are_inherited(summary):
    get_by_id = next(o for o in summary.operations if o.method == "GET" and o.path == "/pets/{petId}")
    names = {p["name"] for p in get_by_id.params}
    assert "petId" in names
    pet_id = next(p for p in get_by_id.params if p["name"] == "petId")
    assert pet_id["in"] == "path" and pet_id["required"] is True


def test_request_body_schema_is_resolved(summary):
    create = next(o for o in summary.operations if o.method == "POST" and o.path == "/pets")
    assert create.request_body is not None
    # $ref to NewPet resolved to its properties, with required marked by '*'.
    assert "name*" in create.request_body


def test_global_security_applies_and_can_be_overridden(summary):
    listing = next(o for o in summary.operations if o.method == "GET" and o.path == "/pets")
    login = next(o for o in summary.operations if o.path == "/login")
    assert listing.security == ["bearerAuth"]  # inherited global security
    assert login.security == []  # explicitly opted out


def test_render_summary_is_prompt_ready(summary):
    text = render_summary(summary)
    assert "POST /pets" in text
    assert "Base URL: https://api.petstore.example/v1" in text
    assert "body:" in text


def test_load_spec_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        load_spec("/nope/does-not-exist.json")


def test_elements_from_summary_is_complete_and_grounded(summary):
    elements = elements_from_summary(summary)
    endpoints = {e.name for e in elements if e.kind == "endpoint"}
    # every operation becomes an element, deterministically (operationId names)
    assert endpoints == {"listPets", "createPet", "getPet", "deletePet", "login"}

    # body fields → inputs with where='body', required preserved
    create = next(e for e in elements if e.name == "createPet")
    body = {i.name: i for i in create.inputs if i.where == "body"}
    assert "name" in body and body["name"].required is True

    # path params mapped; security → precondition; unsecured op has none
    get_pet = next(e for e in elements if e.name == "getPet")
    assert any(i.where == "path" and i.name == "petId" for i in get_pet.inputs)
    listing = next(e for e in elements if e.name == "listPets")
    assert any("bearerAuth" in p for p in listing.preconditions)
    login = next(e for e in elements if e.name == "login")
    assert login.preconditions == []

    # security scheme → deterministic auth element
    assert any(e.kind == "auth" and e.name == "bearerAuth" for e in elements)


class _FakeJudge:
    """Stands in for the LLM judgement layer. Returns priorities/auth/journeys only —
    it cannot add or remove elements, which is the whole point of the new design."""

    def __init__(self) -> None:
        self.last_prompt: str | None = None
        self.last_system: str | None = None

    async def generate(self, prompt: str, *, system: str | None = None, label: str = "") -> str:  # pragma: no cover
        return ""

    async def generate_structured(self, prompt, schema: type[T], *, system=None, label: str = "") -> T:
        self.last_prompt = prompt
        self.last_system = system
        return schema(
            system_summary="A small pet store.",
            auth_strategy="bearer",
            # 'ghost' isn't a real element — must be ignored. listPets omitted → default medium.
            high_priority=["createPet"],
            low_priority=["ghost"],
            suggested_journeys=[
                {"name": "Make a pet", "description": "create then read", "priority": "high",
                 "steps": [], "elements": ["createPet", "getPet", "ghost"]},
            ],
        )


async def test_discover_openapi_enumerates_deterministically():
    provider = _FakeJudge()
    inv = await discover_openapi(str(SPEC), provider)

    assert isinstance(inv, CoverageInventory)
    assert inv.source == "openapi"
    assert inv.base_url == "https://api.petstore.example/v1"  # ground truth, not the model
    assert inv.system_name == "Petstore Mini"
    assert inv.auth_strategy == "bearer" and inv.summary == "A small pet store."
    # structured auth scheme captured deterministically from the spec (http bearer)
    assert any(s.type == "http" and s.scheme == "bearer" for s in inv.auth_schemes)

    # all five endpoints present regardless of what the model said
    endpoints = {e.name for e in inv.elements if e.kind == "endpoint"}
    assert endpoints == {"listPets", "createPet", "getPet", "deletePet", "login"}

    # priority from judgement applied; omitted elements default to medium
    assert next(e for e in inv.elements if e.name == "createPet").priority == "high"
    assert next(e for e in inv.elements if e.name == "listPets").priority == "medium"

    # journey element refs filtered to real names ('ghost' dropped)
    j = inv.suggested_journeys[0]
    assert "ghost" not in j.elements and {"createPet", "getPet"} <= set(j.elements)

    # the judgement prompt carried the authoritative element list
    assert "createPet" in (provider.last_prompt or "")
    assert provider.last_system and "authoritative" in provider.last_system.lower()


async def test_discover_openapi_rejects_empty_spec(tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text('{"openapi": "3.0.0", "info": {"title": "x", "version": "1"}, "paths": {}}')
    with pytest.raises(ValueError):
        await discover_openapi(str(empty), _FakeJudge())


def test_build_judgment_prompt_lists_elements():
    summary = summarize_spec(load_spec(str(SPEC)))
    text = build_judgment_prompt(summary, elements_from_summary(summary))
    assert "createPet" in text and "POST /pets" in text
    assert "authoritative" in text


def test_auth_schemes_extraction_keeps_header_name_and_kind():
    spec = {
        "components": {
            "securitySchemes": {
                "petstore_auth": {"type": "oauth2", "flows": {}},
                "api_key": {"type": "apiKey", "name": "api_key", "in": "header"},
                "basic_auth": {"type": "http", "scheme": "basic"},
            }
        }
    }
    schemes = {s.description: s for s in _auth_schemes_from_spec(spec)}
    assert schemes["api_key"].type == "apiKey"
    assert schemes["api_key"].name == "api_key" and schemes["api_key"].location == "header"
    assert schemes["basic_auth"].type == "http" and schemes["basic_auth"].scheme == "basic"
    assert schemes["petstore_auth"].type == "oauth2"


def test_auth_schemes_handles_swagger2_basic():
    # Swagger 2.0 used type 'basic' directly; we normalise to http/basic.
    schemes = _auth_schemes_from_spec({"securityDefinitions": {"b": {"type": "basic"}}})
    assert schemes[0].type == "http" and schemes[0].scheme == "basic"


def test_inputs_capture_examples_from_spec():
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "X", "version": "1"},
        "servers": [{"url": "https://x"}],
        "paths": {
            "/things": {
                "get": {
                    "operationId": "listThings",
                    "parameters": [
                        {"name": "q", "in": "query", "schema": {"type": "string"}, "example": "hello"}
                    ],
                },
                "post": {
                    "operationId": "makeThing",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["title"],
                                    "properties": {"title": {"type": "string", "example": "Demo"}},
                                }
                            }
                        }
                    },
                },
            }
        },
    }
    elements = elements_from_summary(summarize_spec(spec))
    q = next(i for e in elements for i in e.inputs if i.name == "q")
    assert q.example == "hello" and q.where == "query"
    title = next(i for e in elements for i in e.inputs if i.name == "title")
    assert title.example == "Demo" and title.where == "body"

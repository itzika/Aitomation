"""Core data models for the Discovery Toolkit.

`CoverageInventory` is the central artifact: a structured, validated model of what is
testable in a system. Everything downstream (scaffold, write) consumes it. These are the
shapes the LLM must populate via `generate_structured`, so they are intentionally flat,
well-described, and tolerant of partial data — fields carry descriptions because they
double as the schema the model is steered by.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# Web/API surface: page/form/flow/endpoint/auth. Backend surface added later:
# topic/event_schema (message queues), table/migration (databases).
ElementKind = Literal[
    "page",
    "form",
    "flow",
    "endpoint",
    "auth",
    "topic",
    "event_schema",
    "table",
    "migration",
]
Priority = Literal["high", "medium", "low"]
# `column` = a database column; `message` = a field of an event/message payload.
InputWhere = Literal[
    "query", "path", "header", "cookie", "body", "form", "column", "message", "unknown"
]
DiscoverySource = Literal["openapi", "crawl", "postman", "asyncapi", "schema_registry", "db_schema"]


class InputField(BaseModel):
    """A single input a testable element accepts: a form field, query/path param, or a
    property of a request body."""

    name: str = Field(description="Field or parameter name.")
    type: str = Field(default="string", description="Logical type, e.g. string/integer/boolean.")
    required: bool = Field(default=False, description="Whether the element requires this input.")
    where: InputWhere = Field(
        default="unknown",
        description="Where the input is supplied (query/path/header/cookie/body/form).",
    )
    description: str | None = Field(default=None, description="What this input is for.")
    example: str | None = Field(default=None, description="A concrete example value, if known.")
    locator: str | None = Field(
        default=None,
        description="Observed Playwright locator for web fields, e.g. get_by_placeholder('Email').",
    )
    unique: bool = Field(
        default=True, description="False if the locator matches multiple elements (use .first)."
    )


class TestableElement(BaseModel):
    """One discrete thing worth testing: a page, a form, a multi-step flow, an API
    endpoint, or an auth mechanism."""

    kind: ElementKind = Field(description="What category of surface this is.")
    name: str = Field(description="Short human-readable name, unique within the inventory.")
    location: str = Field(description="Where it lives: a URL path or an endpoint path.")
    description: str = Field(description="What it does and why it is worth testing.")
    method: str | None = Field(
        default=None, description="HTTP method for endpoint elements (GET/POST/...)."
    )
    inputs: list[InputField] = Field(
        default_factory=list, description="Inputs this element accepts."
    )
    preconditions: list[str] = Field(
        default_factory=list,
        description="What must be true first, e.g. 'requires authenticated session'.",
    )
    json_schema: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Raw JSON Schema for the message payload — populated for `event_schema` elements so the "
            "scaffold can emit it and contract tests can validate a sample against it. Unused otherwise."
        ),
    )
    priority: Priority = Field(description="Testing priority relative to the rest of the system.")


class JourneyStep(BaseModel):
    """One step within a suggested user/API journey."""

    action: str = Field(description="What happens in this step, in plain language.")
    target: str | None = Field(
        default=None, description="Name or location of the element this step exercises."
    )


class Journey(BaseModel):
    """A suggested end-to-end path through the system worth covering with a test. These
    seed the Write stage; they are suggestions, never executed assertions."""

    name: str = Field(description="Short name for the journey.")
    description: str = Field(description="What the journey accomplishes and why it matters.")
    priority: Priority = Field(description="How important this journey is to cover.")
    steps: list[JourneyStep] = Field(
        default_factory=list, description="Ordered steps that make up the journey."
    )
    elements: list[str] = Field(
        default_factory=list,
        description="Names of TestableElements this journey touches.",
    )


class AuthScheme(BaseModel):
    """A structured authentication scheme, extracted deterministically from the spec. Ground
    truth for generating the scaffold's auth fixture — distinguishes a bearer token from an
    API key in a custom header, which a free-text `auth_strategy` cannot."""

    type: str = Field(description="Scheme type: apiKey/http/oauth2/openIdConnect, or 'session'.")
    scheme: str | None = Field(default=None, description="For http: 'bearer' or 'basic'.")
    name: str | None = Field(
        default=None,
        description="For apiKey: the header/query/cookie parameter name (e.g. 'api_key').",
    )
    location: str | None = Field(
        default=None, description="For apiKey: where the key goes — header/query/cookie."
    )
    description: str = Field(default="", description="Human description of the scheme.")


class CoverageInventory(BaseModel):
    """The system model. Produced by Discover, consumed by Scaffold and Write."""

    system_name: str = Field(description="Human name of the system under test.")
    base_url: str = Field(description="Base URL or server the system is reached at.")
    source: DiscoverySource = Field(description="How this inventory was discovered.")
    auth_strategy: str | None = Field(
        default=None,
        description="Primary auth mechanism: oauth2/session/basic/bearer/apiKey, or null.",
    )
    auth_schemes: list[AuthScheme] = Field(
        default_factory=list,
        description="Structured auth schemes (ground truth for the scaffold's auth fixture).",
    )
    summary: str = Field(
        default="",
        description="A few sentences describing the system and its testable surface.",
    )
    elements: list[TestableElement] = Field(
        default_factory=list, description="Everything testable that was discovered."
    )
    suggested_journeys: list[Journey] = Field(
        default_factory=list, description="High-value end-to-end paths worth covering."
    )
    generated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="When this inventory was generated.",
    )

    def counts_by_kind(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for el in self.elements:
            out[el.kind] = out.get(el.kind, 0) + 1
        return out

    def counts_by_priority(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for el in self.elements:
            out[el.priority] = out.get(el.priority, 0) + 1
        return out

"""Schema-registry discovery path — the *live* analog of AsyncAPI.

Where AsyncAPI is a static contract file, a Confluent-style schema registry is a running
service that holds the schemas in use. We introspect it over its REST API
(`GET /subjects`, `GET /subjects/{s}/versions/latest`) and turn each subject into an
`event_schema` element — and, when subjects follow the `<topic>-value` / `<topic>-key`
TopicNameStrategy, a `topic` element too. JSON-schema subjects ride along with their
payload schema for contract validation; Avro/Protobuf subjects record their field shape
(full Avro/Protobuf validation is out of this MLP slice).

Same spine as the other paths: deterministic extraction, LLM supplies judgement only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..models import CoverageInventory, InputField, TestableElement
from ..providers import LLMProvider
from .asyncapi import _payload_fields, _unique
from .openapi import InventoryJudgment, render_elements_for_prompt

REQUEST_TIMEOUT = 30.0
MAX_SUBJECTS = 500
_KEY_VALUE_SUFFIXES = ("-value", "-key")


# --------------------------------------------------------------------------------------
# Deterministic extraction (live HTTP)
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class SubjectInfo:
    subject: str
    version: int | None
    schema_type: str  # AVRO | JSON | PROTOBUF
    schema_text: str
    parsed: dict[str, Any] | None = None  # parsed schema JSON (JSON/Avro); None for Protobuf


@dataclass(slots=True)
class RegistrySummary:
    base_url: str
    subjects: list[SubjectInfo] = field(default_factory=list)
    truncated: bool = False


def fetch_registry(base_url: str, *, client: httpx.Client | None = None) -> RegistrySummary:
    """List subjects and fetch each one's latest schema version. `client` is injectable so the
    HTTP layer can be mocked in tests; otherwise a default client is created and closed here."""
    base = base_url.rstrip("/")
    owns_client = client is None
    client = client or httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True)
    try:
        resp = client.get(f"{base}/subjects")
        resp.raise_for_status()
        names = resp.json()
        if not isinstance(names, list):
            raise ValueError("Registry /subjects did not return a list of subject names.")

        summary = RegistrySummary(base_url=base, truncated=len(names) > MAX_SUBJECTS)
        for name in names[:MAX_SUBJECTS]:
            r = client.get(f"{base}/subjects/{name}/versions/latest")
            r.raise_for_status()
            body = r.json()
            data = body if isinstance(body, dict) else {}
            stype = str(data.get("schemaType") or "AVRO").upper()  # registry omits it for Avro
            text = str(data.get("schema", ""))
            parsed: dict[str, Any] | None = None
            if stype in ("JSON", "AVRO"):
                try:
                    loaded = json.loads(text)
                    parsed = loaded if isinstance(loaded, dict) else None
                except json.JSONDecodeError:
                    parsed = None
            summary.subjects.append(
                SubjectInfo(str(name), data.get("version"), stype, text, parsed)
            )
        return summary
    finally:
        if owns_client:
            client.close()


def _topic_of(subject: str) -> str | None:
    """The Kafka topic a subject belongs to under TopicNameStrategy, or None if the subject
    doesn't follow the `<topic>-value`/`-key` convention."""
    for suffix in _KEY_VALUE_SUFFIXES:
        if subject.endswith(suffix):
            return subject[: -len(suffix)]
    return None


def _avro_fields(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Top-level fields of an Avro record. A field whose type union includes `null` is optional."""
    if schema.get("type") != "record" or not isinstance(schema.get("fields"), list):
        return []
    out: list[dict[str, Any]] = []
    for f in schema["fields"]:
        if not isinstance(f, dict) or "name" not in f:
            continue
        ftype = f.get("type")
        if isinstance(ftype, list):  # union
            required = "null" not in ftype
            label = "|".join(str(t) for t in ftype if t != "null") or "union"
        else:
            required = "default" not in f
            label = str(ftype)
        out.append({"name": f["name"], "type": label, "required": required})
    return out


# --------------------------------------------------------------------------------------
# Deterministic element enumeration
# --------------------------------------------------------------------------------------


def elements_from_registry(summary: RegistrySummary) -> list[TestableElement]:
    """A `topic` per derived topic name, an `event_schema` per subject (JSON subjects carry
    their schema for validation; Avro/Protobuf record field shape only)."""
    elements: list[TestableElement] = []
    seen: set[str] = set()

    # Topics first (deterministic order), deduped across -value/-key subjects.
    topics: dict[str, list[str]] = {}
    for s in summary.subjects:
        topic = _topic_of(s.subject)
        if topic is not None:
            topics.setdefault(topic, []).append(s.subject)
    for topic, subjects in topics.items():
        elements.append(
            TestableElement(
                kind="topic",
                name=_unique(topic, seen),
                location=topic,
                description=f"Topic {topic} (registry subjects: {', '.join(subjects)}).",
                priority="medium",
            )
        )

    for s in summary.subjects:
        if s.schema_type == "JSON" and s.parsed is not None:
            inputs = [
                InputField(
                    name=f["name"],
                    type=str(f.get("type", "string")),
                    required=bool(f["required"]),
                    where="message",
                    example=None if f.get("example") is None else str(f["example"]),
                )
                for f in _payload_fields(s.parsed)
            ]
            json_schema = s.parsed
            desc = f"JSON-schema subject {s.subject} (v{s.version})."
        elif s.schema_type == "AVRO" and s.parsed is not None:
            inputs = [
                InputField(
                    name=f["name"],
                    type=str(f["type"]),
                    required=bool(f["required"]),
                    where="message",
                )
                for f in _avro_fields(s.parsed)
            ]
            json_schema = None
            rec = s.parsed.get("name", s.subject)
            desc = f"Avro subject {s.subject} (v{s.version}, record {rec})."
        else:  # PROTOBUF or unparseable — record presence, not a validatable schema
            inputs = []
            json_schema = None
            desc = f"{s.schema_type} subject {s.subject} (v{s.version})."

        elements.append(
            TestableElement(
                kind="event_schema",
                name=_unique(s.subject, seen),
                location=s.subject,
                description=desc,
                inputs=inputs,
                json_schema=json_schema,
                priority="medium",
            )
        )
    return elements


# --------------------------------------------------------------------------------------
# LLM judgement layer
# --------------------------------------------------------------------------------------


_REGISTRY_JUDGMENT_SYSTEM = """\
You are a senior test-automation analyst. You are given the COMPLETE, authoritative list of
testable elements introspected from a schema registry: `event_schema` elements (subjects) and
`topic` elements derived from subject naming. The list is exhaustive and correct: you must NOT
add, remove, rename, or invent elements.

Your job is judgement only:
- Prioritise by exception: list the HIGH-priority and LOW-priority element names (exact names;
  omit mediums). Core domain event subjects are usually high.
- auth_strategy is usually null; set it only if clearly indicated.
- Write a concise system summary.
- Propose 5-10 realistic CONTRACT-check journeys (e.g. "a sample payload conforms to subject
  X's schema"), referencing element names EXACTLY. Do not invent flows beyond the schemas.
"""


def build_registry_judgment_prompt(
    summary: RegistrySummary, elements: list[TestableElement]
) -> str:
    return (
        f"Schema registry: {summary.base_url}\n"
        f"Testable elements ({len(elements)}) — this list is complete and authoritative:\n"
        f"{render_elements_for_prompt(elements)}\n\n"
        "Provide the HIGH-priority and LOW-priority element names (by exact name; omit mediums), "
        "the auth_strategy, a system summary, and 5-10 suggested contract-check journeys "
        "referencing these element names."
    )


async def discover_registry(
    source: str, provider: LLMProvider, *, client: httpx.Client | None = None
) -> CoverageInventory:
    """Schema-registry discovery: fetch subjects/schemas -> enumerate elements deterministically
    -> LLM supplies judgement -> merge into a validated inventory."""
    summary = fetch_registry(source, client=client)
    if not summary.subjects:
        raise ValueError(f"Registry at {source} reported no subjects; nothing to discover.")

    elements = elements_from_registry(summary)
    names = {e.name for e in elements}

    judgment = await provider.generate_structured(
        build_registry_judgment_prompt(summary, elements),
        InventoryJudgment,
        system=_REGISTRY_JUDGMENT_SYSTEM,
        label="discover.registry",
    )

    priorities = judgment.priority_map(names)
    for el in elements:
        el.priority = priorities.get(el.name, el.priority)
    for journey in judgment.suggested_journeys:
        journey.elements = [n for n in journey.elements if n in names]

    return CoverageInventory(
        system_name=f"Schema Registry ({summary.base_url})",
        base_url=summary.base_url,
        source="schema_registry",
        auth_strategy=judgment.auth_strategy,
        summary=judgment.system_summary,
        elements=elements,
        suggested_journeys=judgment.suggested_journeys,
    )

"""AsyncAPI discovery path — the event-driven analog of the OpenAPI path.

AsyncAPI is to message queues what OpenAPI is to REST: a declarative contract for channels
(topics) and the message payloads that flow over them. We extract that surface
*deterministically* — channels become `topic` elements, distinct messages become
`event_schema` elements carrying their resolved JSON Schema — and hand only judgement
(priorities, suggested journeys, a summary) to the LLM. Same division as `openapi.py`: the
model never invents the surface.

Both AsyncAPI 2.x and 3.x are supported. They model operations differently — 2.x nests
`publish`/`subscribe` under each channel; 3.x lifts `send`/`receive` operations to the top
level and references channels — so extraction branches on the declared version, but both
collapse onto the same channel+message shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..models import CoverageInventory, InputField, TestableElement
from ..providers import LLMProvider
from .openapi import InventoryJudgment, _resolve_ref, load_spec, render_elements_for_prompt

# Keep prompts bounded so large specs don't blow the context window.
MAX_CHANNELS = 200
MAX_MESSAGE_PROPS = 30
_MAX_INLINE_DEPTH = 8  # guard against recursive ($ref-cyclic) schemas when inlining


# --------------------------------------------------------------------------------------
# Deterministic extraction
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class MessageInfo:
    name: str
    title: str | None = None
    summary: str | None = None
    content_type: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)  # self-contained JSON Schema
    fields: list[dict[str, Any]] = field(default_factory=list)  # top-level payload props
    channels: list[str] = field(default_factory=list)  # channels this message appears on


@dataclass(slots=True)
class ChannelInfo:
    name: str  # the channel key
    address: str  # the wire address / topic name
    description: str | None = None
    operations: list[str] = field(default_factory=list)  # publish/subscribe or send/receive
    message_names: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AsyncSummary:
    title: str
    version: str
    asyncapi_version: str
    base_url: str
    channels: list[ChannelInfo] = field(default_factory=list)
    messages: dict[str, MessageInfo] = field(default_factory=dict)
    truncated: bool = False


def _spec_major(spec: dict[str, Any]) -> int:
    """Major AsyncAPI version (2 or 3). Defaults to 2 if unparseable."""
    raw = str(spec.get("asyncapi", "2")).strip()
    try:
        return int(raw.split(".", 1)[0])
    except ValueError:
        return 2


def _inline_schema(spec: dict[str, Any], schema: Any, depth: int = 0) -> Any:
    """Recursively resolve local `$ref`s so the stored payload is a self-contained JSON Schema
    a generated contract test can validate against without the source doc. Bounded depth keeps
    recursive schemas from looping forever."""
    if depth >= _MAX_INLINE_DEPTH or not isinstance(schema, dict):
        return schema if isinstance(schema, (dict, list, str, int, float, bool)) else {}
    if "$ref" in schema:
        resolved = _resolve_ref(spec, schema["$ref"])
        return _inline_schema(spec, resolved, depth + 1) if resolved else {}
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "properties" and isinstance(value, dict):
            out[key] = {k: _inline_schema(spec, v, depth + 1) for k, v in value.items()}
        elif key in ("items", "additionalProperties", "not"):
            out[key] = _inline_schema(spec, value, depth + 1)
        elif key in ("allOf", "anyOf", "oneOf") and isinstance(value, list):
            out[key] = [_inline_schema(spec, v, depth + 1) for v in value]
        else:
            out[key] = value
    return out


def _payload_fields(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Top-level properties of an (already inlined) payload schema."""
    props = payload.get("properties")
    if not isinstance(props, dict):
        return []
    required = set(payload.get("required", []))
    out: list[dict[str, Any]] = []
    for pname, pschema in list(props.items())[:MAX_MESSAGE_PROPS]:
        ptype = pschema.get("type", "object") if isinstance(pschema, dict) else "object"
        example = pschema.get("example") if isinstance(pschema, dict) else None
        out.append(
            {"name": pname, "type": ptype, "required": pname in required, "example": example}
        )
    return out


def _message_key(spec: dict[str, Any], raw: dict[str, Any], fallback: str) -> str:
    """A stable, human name for a message — its component key when referenced, else its
    declared name/title, else a channel-derived fallback."""
    if isinstance(raw, dict) and "$ref" in raw:
        return raw["$ref"].rsplit("/", 1)[-1]
    if isinstance(raw, dict):
        return str(raw.get("name") or raw.get("title") or fallback)
    return fallback


def _collect_message(
    spec: dict[str, Any], raw: Any, channel: str, into: dict[str, MessageInfo], fallback: str
) -> str | None:
    """Resolve one message (possibly a `$ref`), record it once in `into`, and return its key.
    A message reused across channels is recorded a single time with all channels noted."""
    if not isinstance(raw, dict):
        return None
    key = _message_key(spec, raw, fallback)
    resolved = _resolve_ref(spec, raw["$ref"]) if "$ref" in raw else raw
    if not isinstance(resolved, dict):
        return None

    existing = into.get(key)
    if existing is not None:
        if channel not in existing.channels:
            existing.channels.append(channel)
        return key

    payload = _inline_schema(spec, resolved.get("payload", {}))
    into[key] = MessageInfo(
        name=key,
        title=resolved.get("title"),
        summary=resolved.get("summary") or resolved.get("description"),
        content_type=resolved.get("contentType"),
        payload=payload if isinstance(payload, dict) else {},
        fields=_payload_fields(payload if isinstance(payload, dict) else {}),
        channels=[channel],
    )
    return key


def _messages_from_node(node: Any) -> list[Any]:
    """A channel/operation `message` node may be a single message, a 2.x `oneOf` list, or a
    3.x `messages` map/list. Normalise to a flat list of raw message nodes."""
    if isinstance(node, dict):
        if isinstance(node.get("oneOf"), list):
            return list(node["oneOf"])
        return [node]
    if isinstance(node, list):
        return list(node)
    return []


def _extract_base_url(spec: dict[str, Any]) -> str:
    servers = spec.get("servers")
    if isinstance(servers, dict) and servers:
        first = next(iter(servers.values()))
        if isinstance(first, dict):
            # 2.x uses `url`; 3.x splits into host/protocol/pathname.
            if first.get("url"):
                return str(first["url"])
            host = first.get("host")
            if host:
                proto = first.get("protocol", "")
                path = first.get("pathname", "")
                return f"{proto + '://' if proto else ''}{host}{path}"
    return "/"


def _summarize_v2(spec: dict[str, Any], summary: AsyncSummary) -> None:
    channels = spec.get("channels", {})
    if not isinstance(channels, dict):
        return
    for chname, item in channels.items():
        if not isinstance(item, dict):
            continue
        if len(summary.channels) >= MAX_CHANNELS:
            summary.truncated = True
            return
        ch = ChannelInfo(name=chname, address=chname, description=item.get("description"))
        for op in ("publish", "subscribe"):
            opnode = item.get(op)
            if not isinstance(opnode, dict):
                continue
            ch.operations.append(op)
            for raw in _messages_from_node(opnode.get("message")):
                key = _collect_message(
                    spec, raw, chname, summary.messages, fallback=f"{chname}_{op}"
                )
                if key and key not in ch.message_names:
                    ch.message_names.append(key)
        summary.channels.append(ch)


def _summarize_v3(spec: dict[str, Any], summary: AsyncSummary) -> None:
    channels = spec.get("channels", {})
    by_key: dict[str, ChannelInfo] = {}
    if isinstance(channels, dict):
        for chname, item in channels.items():
            if not isinstance(item, dict):
                continue
            if len(summary.channels) >= MAX_CHANNELS:
                summary.truncated = True
                break
            ch = ChannelInfo(
                name=chname,
                address=str(item.get("address") or chname),
                description=item.get("description"),
            )
            msgs = item.get("messages")
            if isinstance(msgs, dict):
                for mkey, raw in msgs.items():
                    key = _collect_message(spec, raw, ch.address, summary.messages, fallback=mkey)
                    if key and key not in ch.message_names:
                        ch.message_names.append(key)
            by_key[chname] = ch
            summary.channels.append(ch)

    # Operations (top-level in 3.x) carry the action verb; attribute it to the channel.
    operations = spec.get("operations", {})
    if isinstance(operations, dict):
        for opnode in operations.values():
            if not isinstance(opnode, dict):
                continue
            action = opnode.get("action")  # send | receive
            chref = opnode.get("channel", {})
            chname = chref.get("$ref", "").rsplit("/", 1)[-1] if isinstance(chref, dict) else ""
            ch = by_key.get(chname)
            if ch is not None and isinstance(action, str) and action not in ch.operations:
                ch.operations.append(action)


def summarize_asyncapi(spec: dict[str, Any]) -> AsyncSummary:
    """Deterministically reduce a full AsyncAPI doc to a compact, prompt-ready summary."""
    info = spec.get("info", {}) if isinstance(spec.get("info"), dict) else {}
    summary = AsyncSummary(
        title=str(info.get("title", "Unknown event API")),
        version=str(info.get("version", "")),
        asyncapi_version=str(spec.get("asyncapi", "")),
        base_url=_extract_base_url(spec),
    )
    if _spec_major(spec) >= 3:
        _summarize_v3(spec, summary)
    else:
        _summarize_v2(spec, summary)
    return summary


# --------------------------------------------------------------------------------------
# Deterministic element enumeration
# --------------------------------------------------------------------------------------


def _unique(name: str, seen: set[str]) -> str:
    candidate, n = name, 2
    while candidate in seen:
        candidate, n = f"{name}_{n}", n + 1
    seen.add(candidate)
    return candidate


def elements_from_async_summary(summary: AsyncSummary) -> list[TestableElement]:
    """Build the authoritative element list deterministically: a `topic` per channel and an
    `event_schema` per distinct message (carrying its resolved JSON Schema)."""
    elements: list[TestableElement] = []
    seen: set[str] = set()

    for ch in summary.channels:
        verbs = "|".join(ch.operations) if ch.operations else None
        msgs = ", ".join(ch.message_names) if ch.message_names else "no declared messages"
        desc = ch.description or f"Channel {ch.address}"
        elements.append(
            TestableElement(
                kind="topic",
                name=_unique(ch.name, seen),
                location=ch.address,
                method=verbs,
                description=f"{desc} (messages: {msgs}).",
                priority="medium",
            )
        )

    for msg in summary.messages.values():
        inputs = [
            InputField(
                name=f["name"],
                type=str(f.get("type", "string")),
                required=bool(f["required"]),
                where="message",
                example=None if f.get("example") is None else str(f["example"]),
            )
            for f in msg.fields
        ]
        on = ", ".join(msg.channels) if msg.channels else "—"
        elements.append(
            TestableElement(
                kind="event_schema",
                name=_unique(msg.name, seen),
                location=msg.channels[0] if msg.channels else msg.name,
                description=(msg.summary or msg.title or f"Message {msg.name}") + f" (on: {on}).",
                inputs=inputs,
                json_schema=msg.payload or None,
                priority="medium",
            )
        )
    return elements


# --------------------------------------------------------------------------------------
# LLM judgement layer (priorities, journeys — NOT the surface)
# --------------------------------------------------------------------------------------


_ASYNC_JUDGMENT_SYSTEM = """\
You are a senior test-automation analyst. You are given the COMPLETE, authoritative list of
testable elements for an event-driven system, extracted deterministically from its AsyncAPI
spec: `topic` elements (channels) and `event_schema` elements (message payloads). The list is
exhaustive and correct: you must NOT add, remove, rename, or invent elements.

Your job is judgement only:
- Prioritise by exception: list the HIGH-priority element names and the LOW-priority element
  names. Everything you DON'T list is treated as medium, so only name the ones that genuinely
  stand out. Core domain events and the channels that carry them are usually high. Copy names
  EXACTLY; do not relist mediums.
- auth_strategy is usually null for a message contract; set it only if the spec clearly
  indicates one.
- Write a concise system summary.
- Propose 5-10 realistic journeys. For an event system a good journey is a CONTRACT check —
  e.g. "a sample OrderCreated payload conforms to its schema" — referencing event_schema /
  topic element names EXACTLY. Do not invent cross-service flows the spec doesn't describe.
"""


def build_async_judgment_prompt(summary: AsyncSummary, elements: list[TestableElement]) -> str:
    return (
        f"Event API: {summary.title} (version {summary.version or 'n/a'}, "
        f"AsyncAPI {summary.asyncapi_version or 'n/a'})\n"
        f"Server: {summary.base_url}\n\n"
        f"Testable elements ({len(elements)}) — this list is complete and authoritative:\n"
        f"{render_elements_for_prompt(elements)}\n\n"
        "Provide: the HIGH-priority and LOW-priority element names (by exact name; omit mediums), "
        "the auth_strategy, a system summary, and 5-10 suggested journeys referencing these "
        "element names."
    )


async def discover_asyncapi(source: str, provider: LLMProvider) -> CoverageInventory:
    """AsyncAPI discovery: load -> summarize -> enumerate elements deterministically -> LLM
    supplies judgement (priorities/journeys) -> merge into a validated inventory."""
    spec = load_spec(source)
    summary = summarize_asyncapi(spec)
    if not summary.channels and not summary.messages:
        raise ValueError("No channels or messages found in spec; nothing to discover.")

    elements = elements_from_async_summary(summary)
    names = {e.name for e in elements}

    judgment = await provider.generate_structured(
        build_async_judgment_prompt(summary, elements),
        InventoryJudgment,
        system=_ASYNC_JUDGMENT_SYSTEM,
        label="discover.asyncapi",
    )

    priorities = judgment.priority_map(names)
    for el in elements:
        el.priority = priorities.get(el.name, el.priority)
    for journey in judgment.suggested_journeys:
        journey.elements = [n for n in journey.elements if n in names]

    return CoverageInventory(
        system_name=summary.title,
        base_url=summary.base_url,
        source="asyncapi",
        auth_strategy=judgment.auth_strategy,
        summary=judgment.system_summary,
        elements=elements,
        suggested_journeys=judgment.suggested_journeys,
    )

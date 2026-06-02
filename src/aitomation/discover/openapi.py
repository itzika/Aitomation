"""OpenAPI / Swagger discovery path.

The deterministic-input path, built first per the spec's build order. Endpoints, methods,
params and schemas are extracted *deterministically* from the spec so the LLM never has to
invent surface area — it only adds judgement (priorities, auth inference, suggested
journeys, descriptions) on top of facts it was handed. That division is what keeps the
inventory grounded instead of hallucinated.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
import yaml
from pydantic import BaseModel, Field

from ..models import (
    AuthScheme,
    CoverageInventory,
    InputField,
    InputWhere,
    Journey,
    Priority,
    TestableElement,
)
from ..providers import LLMProvider

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options", "trace")

# Keep prompts bounded so large specs don't blow the context window.
MAX_OPERATIONS = 200
MAX_BODY_PROPS = 20


# --------------------------------------------------------------------------------------
# Deterministic extraction
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class Operation:
    method: str
    path: str
    operation_id: str | None = None
    summary: str | None = None
    description: str | None = None
    tags: list[str] = field(default_factory=list)
    params: list[dict[str, Any]] = field(default_factory=list)
    request_body: str | None = None
    body_fields: list[dict[str, Any]] = field(default_factory=list)
    security: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SpecSummary:
    title: str
    version: str
    base_url: str
    security_schemes: dict[str, str] = field(default_factory=dict)
    operations: list[Operation] = field(default_factory=list)
    truncated: bool = False


def load_spec(source: str) -> dict[str, Any]:
    """Load an OpenAPI/Swagger document from a URL or local path (JSON or YAML)."""
    if source.startswith(("http://", "https://")):
        resp = httpx.get(source, follow_redirects=True, timeout=30.0)
        resp.raise_for_status()
        text = resp.text
    else:
        path = Path(source)
        if not path.is_file():
            raise FileNotFoundError(f"Spec not found: {source}")
        text = path.read_text(encoding="utf-8")

    # YAML is a superset of JSON, so yaml.safe_load handles both. Try JSON first for
    # clearer errors on malformed JSON specs.
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = yaml.safe_load(text)

    if not isinstance(data, dict):
        raise ValueError("Spec did not parse to an object; not a valid OpenAPI document.")
    return data


def _resolve_ref(spec: dict[str, Any], ref: str) -> dict[str, Any]:
    """Resolve a local JSON pointer ($ref) like '#/components/schemas/Foo'."""
    if not ref.startswith("#/"):
        return {}
    node: Any = spec
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or part not in node:
            return {}
        node = node[part]
    return node if isinstance(node, dict) else {}


def _schema_label(spec: dict[str, Any], schema: dict[str, Any], depth: int = 0) -> str:
    """Render a compact one-line label for a JSON schema (resolving one level of $ref)."""
    if "$ref" in schema:
        ref = schema["$ref"]
        name = ref.rsplit("/", 1)[-1]
        if depth == 0:
            resolved = _resolve_ref(spec, ref)
            inner = _schema_label(spec, resolved, depth + 1)
            return f"{name}{inner}" if inner.startswith("{") else name
        return name

    stype = schema.get("type")
    if stype == "array":
        items = schema.get("items", {})
        return f"array<{_schema_label(spec, items, depth + 1)}>"

    props = schema.get("properties")
    if isinstance(props, dict):
        required = set(schema.get("required", []))
        parts = []
        for pname, pschema in list(props.items())[:MAX_BODY_PROPS]:
            ptype = pschema.get("type", "object") if isinstance(pschema, dict) else "object"
            flag = "*" if pname in required else ""
            parts.append(f"{pname}{flag}:{ptype}")
        suffix = ", ..." if len(props) > MAX_BODY_PROPS else ""
        return "{" + ", ".join(parts) + suffix + "}"

    return stype or "object"


def _extract_base_url(spec: dict[str, Any]) -> str:
    servers = spec.get("servers")
    if isinstance(servers, list) and servers and isinstance(servers[0], dict):
        return str(servers[0].get("url", "")).strip() or "/"
    # Swagger 2.0 fallback.
    host = spec.get("host")
    if host:
        scheme = (spec.get("schemes") or ["https"])[0]
        return f"{scheme}://{host}{spec.get('basePath', '')}"
    return "/"


def _extract_security_schemes(spec: dict[str, Any]) -> dict[str, str]:
    schemes: dict[str, str] = {}
    components = spec.get("components", {})
    raw = components.get("securitySchemes") if isinstance(components, dict) else None
    raw = raw or spec.get("securityDefinitions")  # Swagger 2.0
    if isinstance(raw, dict):
        for name, defn in raw.items():
            if not isinstance(defn, dict):
                continue
            stype = defn.get("type", "?")
            extra = defn.get("scheme") or defn.get("flows") or defn.get("in")
            schemes[name] = f"{stype}" + (f" ({extra})" if extra and isinstance(extra, str) else "")
    return schemes


def _auth_schemes_from_spec(spec: dict[str, Any]) -> list[AuthScheme]:
    """Extract structured auth schemes (type + header name + location) from the spec.

    This is what lets the scaffold emit the *correct* fixture: an `apiKey` in an `api_key`
    header is not a `Bearer` token, and `http basic` differs again."""
    components = spec.get("components", {})
    raw = components.get("securitySchemes") if isinstance(components, dict) else None
    raw = raw or spec.get("securityDefinitions")  # Swagger 2.0
    if not isinstance(raw, dict):
        return []

    schemes: list[AuthScheme] = []
    for sname, defn in raw.items():
        if not isinstance(defn, dict):
            continue
        stype = str(defn.get("type", "")).strip()
        # Swagger 2.0 used type 'basic' directly; normalise to http/basic.
        if stype == "basic":
            schemes.append(AuthScheme(type="http", scheme="basic", description=sname))
            continue
        schemes.append(
            AuthScheme(
                type=stype or "unknown",
                scheme=defn.get("scheme"),
                name=defn.get("name"),  # apiKey header/param name
                location=defn.get("in"),  # header/query/cookie
                description=sname,
            )
        )
    return schemes


def _params_for(
    spec: dict[str, Any], op: dict[str, Any], path_params: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in [*path_params, *op.get("parameters", [])]:
        param = _resolve_ref(spec, raw["$ref"]) if isinstance(raw, dict) and "$ref" in raw else raw
        if not isinstance(param, dict) or "name" not in param:
            continue
        schema = param.get("schema", {})
        out.append(
            {
                "name": param["name"],
                "in": param.get("in", "query"),
                "required": bool(param.get("required", param.get("in") == "path")),
                "type": schema.get("type", param.get("type", "string")),
                "example": param.get("example", schema.get("example")),
            }
        )
    return out


def summarize_spec(spec: dict[str, Any]) -> SpecSummary:
    """Deterministically reduce a full OpenAPI doc to a compact, prompt-ready summary."""
    info = spec.get("info", {}) if isinstance(spec.get("info"), dict) else {}
    summary = SpecSummary(
        title=str(info.get("title", "Unknown API")),
        version=str(info.get("version", "")),
        base_url=_extract_base_url(spec),
        security_schemes=_extract_security_schemes(spec),
    )

    paths = spec.get("paths", {})
    if not isinstance(paths, dict):
        return summary

    global_security = _security_names(spec.get("security"))

    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        shared_params = item.get("parameters", [])
        for method in HTTP_METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            if len(summary.operations) >= MAX_OPERATIONS:
                summary.truncated = True
                return summary

            # An absent `security` inherits the global one; an explicit (even empty)
            # `security` overrides it — `security: []` means "no auth required here".
            op_security = op.get("security")
            security = global_security if op_security is None else _security_names(op_security)

            summary.operations.append(
                Operation(
                    method=method.upper(),
                    path=path,
                    operation_id=op.get("operationId"),
                    summary=op.get("summary"),
                    description=op.get("description"),
                    tags=list(op.get("tags", [])),
                    params=_params_for(spec, op, shared_params),
                    request_body=_request_body_label(spec, op),
                    body_fields=_request_body_fields(spec, op),
                    security=security,
                )
            )
    return summary


def _security_names(security: Any) -> list[str]:
    if not isinstance(security, list):
        return []
    names: list[str] = []
    for entry in security:
        if isinstance(entry, dict):
            names.extend(entry.keys())
    return names


def _request_body_label(spec: dict[str, Any], op: dict[str, Any]) -> str | None:
    body = op.get("requestBody")
    if isinstance(body, dict) and "$ref" in body:
        body = _resolve_ref(spec, body["$ref"])
    if not isinstance(body, dict):
        return None
    content = body.get("content")
    if not isinstance(content, dict):
        return None
    # Prefer JSON, else take whatever's first.
    media = content.get("application/json") or next(iter(content.values()), None)
    if not isinstance(media, dict):
        return None
    schema = media.get("schema")
    if not isinstance(schema, dict):
        return None
    return _schema_label(spec, schema)


def _request_body_fields(spec: dict[str, Any], op: dict[str, Any]) -> list[dict[str, Any]]:
    """Structured top-level properties of the JSON request body (name/type/required)."""
    body = op.get("requestBody")
    if isinstance(body, dict) and "$ref" in body:
        body = _resolve_ref(spec, body["$ref"])
    if not isinstance(body, dict):
        return []
    content = body.get("content")
    if not isinstance(content, dict):
        return []
    media = content.get("application/json") or next(iter(content.values()), None)
    if not isinstance(media, dict):
        return []
    schema = media.get("schema")
    if isinstance(schema, dict) and "$ref" in schema:
        schema = _resolve_ref(spec, schema["$ref"])
    if not isinstance(schema, dict):
        return []
    props = schema.get("properties")
    if not isinstance(props, dict):
        return []
    required = set(schema.get("required", []))
    fields: list[dict[str, Any]] = []
    for pname, pschema in list(props.items())[:MAX_BODY_PROPS]:
        ptype = pschema.get("type", "object") if isinstance(pschema, dict) else "object"
        example = pschema.get("example") if isinstance(pschema, dict) else None
        fields.append({"name": pname, "type": ptype, "required": pname in required, "example": example})
    return fields


def render_summary(summary: SpecSummary) -> str:
    """Render the summary as compact text for the discovery prompt."""
    lines: list[str] = [
        f"API: {summary.title} (version {summary.version or 'n/a'})",
        f"Base URL: {summary.base_url}",
    ]
    if summary.security_schemes:
        schemes = "; ".join(f"{k}={v}" for k, v in summary.security_schemes.items())
        lines.append(f"Security schemes: {schemes}")
    else:
        lines.append("Security schemes: none declared")
    if summary.truncated:
        lines.append(f"(NOTE: truncated to first {MAX_OPERATIONS} operations)")
    lines.append("")
    lines.append(f"Operations ({len(summary.operations)}):")

    for op in summary.operations:
        head = f"- {op.method} {op.path}"
        if op.summary:
            head += f" — {op.summary}"
        lines.append(head)
        if op.tags:
            lines.append(f"    tags: {', '.join(op.tags)}")
        if op.params:
            rendered = ", ".join(
                f"{p['name']}({p['in']}{'*' if p['required'] else ''}:{p['type']})"
                for p in op.params
            )
            lines.append(f"    params: {rendered}")
        if op.request_body:
            lines.append(f"    body: {op.request_body}")
        if op.security:
            lines.append(f"    secured by: {', '.join(op.security)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Deterministic element enumeration
# --------------------------------------------------------------------------------------
#
# Endpoints are ground truth: they are parsed from the spec, never invented or dropped by
# the model. This enforces the moat ("the inventory IS the system model") instead of merely
# asserting it, eliminates run-to-run completeness variance, and keeps discovery usable even
# with weak local models — they only supply judgement, not the surface.


def _where(location: str) -> InputWhere:
    return location if location in ("query", "path", "header", "cookie") else "unknown"  # type: ignore[return-value]


def _op_name(method: str, path: str) -> str:
    slug = re.sub(r"[^0-9a-zA-Z]+", "_", path.replace("{", "").replace("}", "")).strip("_").lower()
    return f"{method.lower()}_{slug}" if slug else method.lower()


def _example_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _unique(name: str, seen: set[str]) -> str:
    candidate, n = name, 2
    while candidate in seen:
        candidate, n = f"{name}_{n}", n + 1
    seen.add(candidate)
    return candidate


def elements_from_summary(summary: SpecSummary) -> list[TestableElement]:
    """Build the complete, authoritative element list deterministically from the spec."""
    elements: list[TestableElement] = []
    seen: set[str] = set()

    for op in summary.operations:
        name = _unique(op.operation_id or _op_name(op.method, op.path), seen)
        inputs = [
            InputField(
                name=p["name"],
                type=str(p.get("type", "string")),
                required=bool(p["required"]),
                where=_where(p["in"]),
                example=_example_str(p.get("example")),
            )
            for p in op.params
        ]
        inputs += [
            InputField(
                name=f["name"],
                type=str(f.get("type", "string")),
                required=bool(f["required"]),
                where="body",
                example=_example_str(f.get("example")),
            )
            for f in op.body_fields
        ]
        elements.append(
            TestableElement(
                kind="endpoint",
                name=name,
                location=op.path,
                method=op.method,
                description=op.summary or op.description or f"{op.method} {op.path}",
                inputs=inputs,
                preconditions=[f"requires {s}" for s in op.security],
                priority="medium",  # overwritten by the judgement layer
            )
        )

    for scheme_name, desc in summary.security_schemes.items():
        elements.append(
            TestableElement(
                kind="auth",
                name=_unique(scheme_name, seen),
                location=desc or "auth",
                description=f"Authentication scheme: {scheme_name} ({desc}).",
                priority="medium",
            )
        )
    return elements


# --------------------------------------------------------------------------------------
# LLM judgement layer (priorities, auth inference, journeys — NOT the surface)
# --------------------------------------------------------------------------------------


class InventoryJudgment(BaseModel):
    """What the model is allowed to decide. It never decides which elements exist.

    Priorities are returned as *exceptions only* — the names that are high and the names that
    are low. Everything unlisted defaults to medium. Most elements are medium, so this avoids
    echoing every element name back just to label it 'medium' (a large, pure-waste output
    saving on big specs); it also keeps the schema flat (two string lists, no nested objects)."""

    system_summary: str = Field(description="A few sentences on the system and its testable surface.")
    auth_strategy: str | None = Field(
        default=None,
        description="Primary auth mechanism inferred from the schemes (oauth2/bearer/apiKey/basic), or null.",
    )
    high_priority: list[str] = Field(
        default_factory=list,
        description="Names of HIGH-priority elements (exact names). Omit anything that is medium.",
    )
    low_priority: list[str] = Field(
        default_factory=list,
        description="Names of LOW-priority elements (exact names). Omit anything that is medium.",
    )
    suggested_journeys: list[Journey] = Field(
        default_factory=list,
        description="5-10 high-value end-to-end journeys, each referencing element names.",
    )

    def priority_map(self, valid_names: set[str]) -> dict[str, Priority]:
        """Resolve the high/low exception lists into a name->priority map, keeping only names
        that actually exist. Unlisted (and any name not in `valid_names`) stays medium."""
        out: dict[str, Priority] = {}
        for n in self.low_priority:
            if n in valid_names:
                out[n] = "low"
        for n in self.high_priority:  # high wins if a name appears in both
            if n in valid_names:
                out[n] = "high"
        return out


_JUDGMENT_SYSTEM = """\
You are a senior test-automation analyst. You are given the COMPLETE, authoritative list of
testable elements for an API, extracted deterministically from its OpenAPI spec. The list is
exhaustive and correct: you must NOT add, remove, rename, or invent elements.

Your job is judgement only:
- Prioritise by exception: list the HIGH-priority element names and the LOW-priority element
  names. Everything you DON'T list is treated as medium, so only name the ones that genuinely
  stand out. State-changing operations (POST/PUT/PATCH/DELETE) and auth are usually high;
  trivial reads are usually low. Copy names EXACTLY; do not relist mediums.
- Infer the primary auth_strategy from the security schemes (oauth2/bearer/apiKey/basic), or
  null if none are declared.
- Write a concise system summary.
- Propose 5-10 realistic end-to-end journeys that chain elements (e.g. create -> read ->
  update -> delete, or authenticate -> act). Reference element names EXACTLY as given.
"""


def render_elements_for_prompt(elements: list[TestableElement]) -> str:
    lines: list[str] = []
    for e in elements:
        loc = f"{e.method} {e.location}" if e.method else e.location
        line = f"- [{e.kind}] {e.name} — {loc}"
        if e.description and e.description != loc:
            line += f": {e.description}"
        lines.append(line)
    return "\n".join(lines)


def build_judgment_prompt(summary: SpecSummary, elements: list[TestableElement]) -> str:
    schemes = (
        "; ".join(f"{k}={v}" for k, v in summary.security_schemes.items())
        or "none declared"
    )
    return (
        f"API: {summary.title} (version {summary.version or 'n/a'})\n"
        f"Base URL: {summary.base_url}\n"
        f"Security schemes: {schemes}\n\n"
        f"Testable elements ({len(elements)}) — this list is complete and authoritative:\n"
        f"{render_elements_for_prompt(elements)}\n\n"
        "Provide: the HIGH-priority and LOW-priority element names (by exact name; omit mediums), "
        "the auth_strategy, a system summary, and 5-10 suggested journeys referencing these "
        "element names."
    )


async def discover_openapi(source: str, provider: LLMProvider) -> CoverageInventory:
    """OpenAPI discovery: load -> summarize -> enumerate elements deterministically ->
    LLM supplies judgement (priorities/auth/journeys) -> merge into a validated inventory."""
    spec = load_spec(source)
    summary = summarize_spec(spec)
    if not summary.operations:
        raise ValueError("No operations found in spec; nothing to discover.")

    # Specs often declare a relative server URL (e.g. "/api/v3"). When we fetched the spec
    # over HTTP, resolve it against that URL so the base_url is actually reachable.
    if source.startswith(("http://", "https://")) and not summary.base_url.startswith(
        ("http://", "https://")
    ):
        summary.base_url = urljoin(source, summary.base_url)

    elements = elements_from_summary(summary)
    names = {e.name for e in elements}

    judgment = await provider.generate_structured(
        build_judgment_prompt(summary, elements),
        InventoryJudgment,
        system=_JUDGMENT_SYSTEM,
        label="discover.openapi",
    )

    # Apply the model's priorities to the deterministic elements (default medium if omitted).
    priorities = judgment.priority_map(names)
    for el in elements:
        el.priority = priorities.get(el.name, el.priority)

    # Keep only journey element references that actually exist — no dangling/invented names.
    for journey in judgment.suggested_journeys:
        journey.elements = [n for n in journey.elements if n in names]

    return CoverageInventory(
        system_name=summary.title,
        base_url=summary.base_url,
        source="openapi",
        auth_strategy=judgment.auth_strategy,
        auth_schemes=_auth_schemes_from_spec(spec),  # ground truth, not the model's call
        summary=judgment.system_summary,
        elements=elements,
        suggested_journeys=judgment.suggested_journeys,
    )

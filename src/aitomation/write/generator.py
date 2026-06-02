"""Turn inventory journeys into first-draft test files.

One LLM call per selected journey, each grounded in only the elements that journey touches
so drafts reference real paths/fields. Output is validated as a Pydantic `TestDraft`, then
syntax-checked: clean drafts land in `tests/`, anything that won't parse is quarantined to
`drafts_needs_review/` so it can't break `pytest` collection.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from ..diff import journey_fingerprint
from ..models import CoverageInventory, Journey, TestableElement
from ..providers import LLMProvider
from ..scaffold.generator import _class_name, _func_name

MAX_JOURNEYS = 8

_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}

# Methods that change server state. Drafts for journeys touching these are emitted but
# SKIPPED by default — a generated DELETE must never run against a real system by accident.
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

_SKIP_BLOCK = (
    "import pytest\n\n"
    "# Safety guard injected by Aitomation: this journey performs mutating requests.\n"
    "pytestmark = pytest.mark.skip(\n"
    '    reason="DESTRUCTIVE journey (mutating request) — review, add teardown, '
    'then remove this skip to enable."\n'
    ")\n\n\n"
)


class TestDraft(BaseModel):
    """A single generated test module for one journey."""

    code: str = Field(description="Complete, importable Python pytest module source.")
    confidence: Literal["high", "medium", "low"] = Field(
        description="How confident the draft is correct and runnable as-is."
    )
    review_notes: str = Field(
        description="What a human reviewer should verify or fix (selectors, values, edge cases)."
    )


@dataclass(slots=True)
class WriteResult:
    journey: str
    path: Path
    confidence: str
    needs_review: bool
    destructive: bool = False
    runtime_failed: bool = False  # --verify ran it and it still failed after one self-heal


@dataclass(slots=True)
class WriteReport:
    written: list[WriteResult] = field(default_factory=list)
    quarantined: list[WriteResult] = field(default_factory=list)
    skipped: list[WriteResult] = field(default_factory=list)  # already existed; left untouched


# --------------------------------------------------------------------------------------
# Journey selection
# --------------------------------------------------------------------------------------


def select_journeys(inv: CoverageInventory, max_journeys: int = MAX_JOURNEYS) -> list[Journey]:
    """Pick journeys to draft, highest priority first. Falls back to synthesising
    one-step journeys from high-priority elements if the inventory suggested none."""
    journeys = sorted(
        inv.suggested_journeys, key=lambda j: _PRIORITY_ORDER.get(j.priority, 3)
    )
    if journeys:
        return journeys[:max_journeys]

    synthesized: list[Journey] = []
    elements = sorted(inv.elements, key=lambda e: _PRIORITY_ORDER.get(e.priority, 3))
    for el in elements[:max_journeys]:
        synthesized.append(
            Journey(
                name=f"Exercise {el.name}",
                description=f"Exercise the {el.kind} '{el.name}' at {el.location}.",
                priority=el.priority,
                elements=[el.name],
            )
        )
    return synthesized


def _elements_for(inv: CoverageInventory, journey: Journey) -> list[TestableElement]:
    by_name = {e.name: e for e in inv.elements}
    matched = [by_name[n] for n in journey.elements if n in by_name]
    return matched or inv.elements


_SUBMIT_VERBS = (
    "submit", "register", "sign up", "signup", "create", "send", "place order",
    "checkout", "subscribe", "log in", "login", "update", "delete", "post ",
)

# Markers of REAL mutation in *generated code* (not journey prose). Backend contract drafts are
# read-only by construction — the prompt forbids DML/publishing and the scaffold provides only
# read-only fixtures (db_inspector / message_schema) — but if a model ignores that and emits an
# actual write or publish, we guard it. Code is a reliable signal; journey descriptions are not
# (a contract check is routinely described as "rejects inserts" / "producers emit valid …").
_DML_MARKERS = (
    "insert into", "update ", "delete from", "drop table", "truncate", "create table",
    ".commit(",  # explicit persist; the scaffold exposes only db_inspector/db_engine (no ORM session)
)
_PUBLISH_MARKERS = (
    ".produce(", ".publish(", ".send(", "kafkaproducer", "confluent_kafka", "aiokafka", "pika.",
)


def _mutates_backend(code: str) -> bool:
    """True if a generated DATA/EVENT draft actually performs a write/publish (vs. read-only
    introspection or schema validation). The post-generation safety net behind is_destructive
    for backend surfaces — see _DML_MARKERS / _PUBLISH_MARKERS."""
    lowered = code.lower()
    return any(m in lowered for m in _DML_MARKERS) or any(m in lowered for m in _PUBLISH_MARKERS)


def is_destructive(inv: CoverageInventory, journey: Journey) -> bool:
    """True if the journey performs a destructive action. API endpoints with a mutating
    method always count. For web forms we flag only **password-bearing** forms or a mutating
    form the journey **explicitly submits** — so an incidental footer/newsletter form on a
    page doesn't flag every journey that merely visits it."""
    by_name = {e.name: e for e in inv.elements}
    referenced = [by_name[n] for n in journey.elements if n in by_name]

    if any(
        e.kind == "endpoint" and (e.method or "").upper() in _MUTATING_METHODS for e in referenced
    ):
        return True

    text = (journey.description + " " + " ".join(s.action for s in journey.steps)).lower()
    submits = any(v in text for v in _SUBMIT_VERBS)
    for e in referenced:
        if e.kind != "form":
            continue
        password = any((i.type or "").lower() == "password" for i in e.inputs)
        mutating = (e.method or "").upper() in _MUTATING_METHODS
        if password or (mutating and submits):
            return True
    # Note: EVENT/DATA (topic/table) journeys are NOT judged here — their contract drafts are
    # read-only by construction. A draft that nonetheless emits real mutation is caught
    # post-generation in draft_tests via _mutates_backend (code, not prose).
    return False


# --------------------------------------------------------------------------------------
# Prompting
# --------------------------------------------------------------------------------------

def journey_type(*, web: bool, api: bool, event: bool = False, data: bool = False) -> str:
    """The surface tags the user prompt declares so the system prompt's per-surface rules
    resolve. Composable: a journey can touch several surfaces (e.g. 'API+EVENT'). One of
    WEB / API / EVENT / DATA or a '+'-joined combination; defaults to API if none apply."""
    tags = [t for t, on in (("WEB", web), ("API", api), ("EVENT", event), ("DATA", data)) if on]
    return "+".join(tags) or "API"


def build_system_prompt() -> str:
    """The single, static system prompt for ALL write/fix/heal calls.

    Deliberately invariant: every rule (web, API, and both) is present once and gated on the
    'Journey type' the user prompt declares. A constant prefix is what lets prompt caching
    actually hit — 4 web/api variants used to fragment the cache and re-bill the prefix every
    call. The few extra tokens of carrying all rules are amortized to ~0 by the cache."""
    return "\n".join([
        "You are a senior test-automation engineer writing a FIRST-DRAFT pytest test for ONE journey",
        "through a system. A human will review it before it is trusted.",
        "",
        "The context below declares a 'Journey type' naming the surfaces this journey touches —",
        "one or a combination of WEB, API, EVENT (message queues), DATA (databases), e.g. 'API+EVENT'.",
        "Apply the rules for EACH surface named and ignore rules for surfaces not present.",
        "",
        "Hard rules (these encode a product guarantee — follow them exactly):",
        "- Output a COMPLETE, importable Python module: imports, a short module docstring naming the",
        "  journey, and the test function(s).",
        "- Use ONLY deterministic assertions: Python `assert` and Playwright `expect(...)`. NEVER call",
        "  an LLM, never make the outcome subjective. The test runner decides pass/fail, not AI.",
        "- Use ONLY the routes, endpoints, fields, forms and inputs given in the context. Do NOT",
        "  invent paths, parameters, or field names.",
        "- Each test must be self-contained and isolated: don't depend on order or leftover state;",
        "  create what you need and (for mutations) clean it up.",
        "- A role/text locator may match MULTIPLE elements (e.g. a nav link repeated in header and",
        "  footer). If so, narrow it (scope to a region) or use `.first` — never leave it ambiguous.",
        "- Name the function test_<snake_case>. Keep it minimal and runnable.",
        "- Where you must guess a value or selector, leave a `# TODO:` comment so the reviewer sees it.",
        "- Do not add explanatory prose outside the code. Return only the module source in `code`.",
        "- Do NOT skip the test yourself — no `pytest.mark.skip`, `xfail`, or module-level `pytestmark`.",
        "  Write it to actually run; the toolkit injects skips for destructive flows on its own.",
        "",
        "For journeys that touch a WEB surface:",
        "- Use the generated Page Objects (`from pages import SomePage`) rather than raw selectors in the",
        "  test — instantiate with the `page` fixture and call their methods. Assert with Playwright's",
        "  web-first `expect(...)` (auto-waiting), never manual sleeps, and prefer role/label/text",
        "  locators. Use the `page` and `base_url` fixtures.",
        "- If you use `expect(...)`, you MUST import it: `from playwright.sync_api import expect`.",
        "",
        "For journeys that touch an API surface:",
        "- Use the `api_request_context` fixture (a Playwright APIRequestContext whose base URL is already",
        "  set and ends with '/'). Call endpoints as paths RELATIVE to that base — i.e. DROP the leading",
        "  slash and do NOT repeat the base URL. Example: for an endpoint listed as `/character/{id}`, call",
        "  `api_request_context.get(f\"character/{character_id}\")`, not `\"/character/{id}\"` and not the",
        "  full URL. Obtain path-param values earlier in the same test (e.g. an id from a list response).",
        "",
        "For journeys with NO web surface:",
        "- Do NOT use the `page` fixture, do NOT import or use Page Objects, and do NOT use Playwright's",
        "  `expect(...)` Web assertions. Do NOT import `expect`.",
        "",
        "For journeys with NO API surface:",
        "- Do NOT use the `api_request_context` fixture.",
        "",
        "For EVENT journeys (topics / event_schema — message contracts):",
        "- Write a CONTRACT test, NOT a broker round-trip. Do NOT connect to Kafka/RabbitMQ/etc., do NOT",
        "  publish or consume. Use the `message_schema` fixture: `schema = message_schema(\"<EventSchemaName>\")`",
        "  returns the discovered JSON Schema for that element (use the EXACT element name from the context).",
        "- Build a sample payload from the message's listed fields (use their example values), then assert it",
        "  conforms: `import jsonschema; jsonschema.validate(instance=sample, schema=schema)` (a passing",
        "  validate raises nothing). You MAY also assert that dropping a REQUIRED field raises",
        "  `jsonschema.exceptions.ValidationError` using `pytest.raises`.",
        "- Deterministic, needs no running broker. Do NOT use `page` or `api_request_context`.",
        "",
        "For DATA journeys (database tables — schema/constraint contracts):",
        "- Write a READ-ONLY schema/constraint test against the live catalog via the `db_inspector` fixture",
        "  (a SQLAlchemy Inspector). Do NOT insert/update/delete rows or run DML — introspect only.",
        "- Useful calls: `db_inspector.get_table_names()`, `get_columns(\"<table>\")` (each a dict with",
        "  'name'/'type'/'nullable'), `get_pk_constraint(t)`, `get_foreign_keys(t)`, `get_unique_constraints(t)`.",
        "- Assert what the inventory states: the table exists, expected columns are present, and the NOT NULL /",
        "  PK / FK / UNIQUE constraints hold. Use the EXACT table name/location from the element.",
        "- Do NOT use `page` or `api_request_context`.",
        "",
        "Playwright Assertions Reference Guide (Python syntax) — for journeys using `expect(...)`:",
        "Use only these valid `expect` assertions (and their negated counterparts starting with `not_to_`):",
        "  expect(locator).to_be_visible() / to_be_hidden()",
        "  expect(locator).to_be_enabled() / to_be_disabled()",
        "  expect(locator).to_be_checked() / to_be_editable() / to_be_empty() / to_be_focused()",
        "  expect(locator).to_contain_text(expected) / to_have_text(expected)",
        "  expect(locator).to_have_attribute(name, value) / to_have_class(expected)",
        "  expect(locator).to_have_id(id) / to_have_value(value) / to_have_values(values)",
        "  expect(locator).to_have_count(count)",
        "  expect(page).to_have_title(title_or_reg) / to_have_url(url_or_reg)",
        "",
        "Assertion Rules:",
        "- Do NOT use imaginary keyword arguments or python logical operators inside `to_have_count()`. ",
        "For example, `expect(locator).to_have_count(greater_than=0)` or `expect(locator).to_have_count(min=1)` ",
        "are INVALID and will fail compilation. Playwright `to_have_count()` only takes an integer `count`.",
        "- To assert that a list contains at least one item, assert that the first item is visible: ",
        "`expect(locator.first).to_be_visible()`. Alternatively, assert that the count is not 0: ",
        "`expect(locator).not_to_have_count(0)`.",
        "",
        "Data & assertion discipline (this is what separates a runnable draft from a brittle one):",
        "- NEVER assume a specific id/username/record already exists. To read or act on a single",
        "  resource, FIRST obtain a real id from a list/collection endpoint in the SAME test, then use",
        "  it. If there is no list endpoint, assert `resp.status in (200, 404)` — not `== 200`.",
        "For API journeys specifically:",
        "- NEVER assume a collection is non-empty: check length before indexing `[0]`; if it might be",
        "  empty, assert the response shape instead of indexing into it.",
        "- For filtered/searched queries, do NOT assume returned field values exactly equal the filter",
        "  input (filters are often partial/case-insensitive). Assert the field is present and, at most,",
        "  that it relates to the filter (e.g. substring) — not strict equality.",
        "- Prefer asserting status, JSON shape, and presence/type of REQUIRED fields over exact values",
        "  you cannot guarantee on a live system. Do not assert exact counts or specific ids.",
        "- Use the provided example values for inputs when they are given.",
        "- Keep assertions MEANINGFUL — still assert what the inventory guarantees (status ranges,",
        "  required fields, response shape). Do not weaken a test into asserting nothing.",
    ])


_SYSTEM_PROMPT = build_system_prompt()



def _render_elements(elements: list[TestableElement]) -> str:
    lines: list[str] = []
    for e in elements:
        head = f"- {e.kind}: {e.name} @ "
        head += f"{e.method + ' ' if e.method else ''}{e.location} (priority: {e.priority})"
        lines.append(head)
        if e.description:
            lines.append(f"    desc: {e.description}")
        if e.inputs:
            rendered = ", ".join(
                f"{i.name}{'*' if i.required else ''}({i.where}:{i.type})"
                + (f"=e.g. {i.example}" if i.example else "")
                for i in e.inputs
            )
            lines.append(f"    inputs: {rendered}")
        if e.preconditions:
            lines.append(f"    preconditions: {', '.join(e.preconditions)}")
    return "\n".join(lines)


def _po_signature(e: TestableElement) -> str:
    """The real generated Page Object API for an element, so the model can't invent methods."""
    name = _class_name(e.name)
    fields = [_func_name(i.name) for i in e.inputs if i.where in ("form", "unknown")]
    if e.kind == "form" and fields:
        locs = ", ".join("." + f for f in fields)
        fills = ", ".join(f"{f}=..." for f in fields)
        return f"  {name}(page): .goto(), .fill({fills}); locators: {locs}"
    return f"  {name}(page): .goto()"


def build_prompt(inv: CoverageInventory, journey: Journey, *, destructive: bool = False) -> str:
    steps = "\n".join(f"  {i + 1}. {s.action}" for i, s in enumerate(journey.steps)) or "  (none)"
    elements = _elements_for(inv, journey)
    # Declare the journey type so the static system prompt's conditional rules resolve.
    web = any(e.kind in ("page", "form") for e in elements)
    api = any(e.kind == "endpoint" for e in elements)
    event = any(e.kind in ("topic", "event_schema") for e in elements)
    data = any(e.kind in ("table", "migration") for e in elements)
    jtype = journey_type(web=web, api=api, event=event, data=data)
    safety = ""
    if destructive:
        safety = (
            "\nThis journey performs MUTATING requests (create/update/delete). Create any data "
            "the test needs, then CLEAN UP afterwards (teardown) so it is repeatable and leaves "
            "no residue. The draft will be skipped by default and reviewed before being enabled.\n"
        )
    # Give the model the EXACT Page Object API so imports resolve AND it can't invent methods.
    pos = [e for e in elements if e.kind in ("page", "form")]
    po_line = ""
    if pos:
        sigs = "\n".join(_po_signature(e) for e in pos)
        po_line = (
            "\nPage Objects (import from `pages`). Import ONLY the one(s) this test actually "
            "drives — no unused imports. Use only their listed methods/locators; navigate with "
            ".goto(), never invent methods like .navigate():\n"
            f"{sigs}\n"
        )
    # Ground backend surfaces on the exact fixtures + element names so drafts can't invent them.
    ev_line = ""
    if event:
        names = [e.name for e in elements if e.kind == "event_schema"]
        if names:
            ev_line = (
                "\nEvent schemas — call `message_schema(\"<name>\")` for the JSON Schema, then "
                "validate a sample with jsonschema. Available: " + ", ".join(names) + ".\n"
            )
    db_line = ""
    if data:
        tnames = [e.location for e in elements if e.kind in ("table", "migration")]
        if tnames:
            db_line = (
                "\nTables — introspect read-only with the `db_inspector` fixture (SQLAlchemy "
                "Inspector). Tables: " + ", ".join(tnames) + ".\n"
            )
    return (
        f"Journey type: {jtype}\n"
        f"System: {inv.system_name}\n"
        f"Base URL: {inv.base_url}\n"
        f"Auth strategy: {inv.auth_strategy or 'none'}\n\n"
        f"Journey to cover: {journey.name}\n"
        f"Goal: {journey.description}\n"
        f"Steps:\n{steps}\n\n"
        f"Elements this journey touches:\n{_render_elements(elements)}\n"
        f"{po_line}{ev_line}{db_line}{safety}\n"
        "Write the first-draft pytest module for this journey."
    )


# --------------------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------------------


def _header(inv: CoverageInventory, journey: Journey, draft: TestDraft, destructive: bool) -> str:
    notes = draft.review_notes.replace("\n", " ").strip()
    lines = [
        "# AI FIRST-DRAFT — review before trusting. Generated by Aitomation Write.",
        f"# Journey: {journey.name} — {journey.description}",
        # Stable flow id (the element-set this test covers). Used to recognise the same flow
        # across re-discovers even when the LLM renames the journey — don't edit it.
        f"# Flow: {journey_fingerprint(inv, journey)}",
        f"# Source: {inv.system_name} ({inv.source}) | confidence: {draft.confidence}"
        + (" | DESTRUCTIVE: skipped by default" if destructive else ""),
        f"# Reviewer notes: {notes}" if notes else "# Reviewer notes: (none)",
        "",
        "",
    ]
    return "\n".join(lines)


def _compiles(code: str, name: str) -> bool:
    try:
        compile(code, name, "exec")
        return True
    except SyntaxError:
        return False


_BANNED = ("time.sleep(", "wait_for_timeout(")


def _page_object_usage(code: str) -> tuple[set[str], set[str]]:
    """Return (imported_from_pages, of_those_actually_referenced). Used to catch the
    Goodhart failure where a draft imports a Page Object only to satisfy the lint, then
    never instantiates it."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return set(), set()
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "pages":
            imported.update(alias.asname or alias.name for alias in node.names)
    used = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id in imported
    }
    return imported, used


_VALID_ASSERTIONS = {
    "to_be_checked", "not_to_be_checked",
    "to_be_disabled", "not_to_be_disabled",
    "to_be_editable", "not_to_be_editable",
    "to_be_empty", "not_to_be_empty",
    "to_be_enabled", "not_to_be_enabled",
    "to_be_focused", "not_to_be_focused",
    "to_be_hidden", "not_to_be_hidden",
    "to_be_visible", "not_to_be_visible",
    "to_contain_text", "not_to_contain_text",
    "to_have_attribute", "not_to_have_attribute",
    "to_have_class", "not_to_have_class",
    "to_have_count", "not_to_have_count",
    "to_have_css", "not_to_have_css",
    "to_have_id", "not_to_have_id",
    "to_have_js_property", "not_to_have_js_property",
    "to_have_text", "not_to_have_text",
    "to_have_value", "not_to_have_value",
    "to_have_values", "not_to_have_values",
    "to_have_title", "not_to_have_title",
    "to_have_url", "not_to_have_url",
}


class PlaywrightAssertionVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.findings: list[str] = []

    def visit_Call(self, node: ast.Call) -> None:
        self.generic_visit(node)
        if not isinstance(node.func, ast.Attribute):
            return

        method_name = node.func.attr
        receiver = node.func.value

        # Check if the receiver is a call to expect(...)
        if isinstance(receiver, ast.Call) and isinstance(receiver.func, ast.Name) and receiver.func.id == "expect":
            if method_name not in _VALID_ASSERTIONS:
                self.findings.append(f"calls non-existent Playwright assertion method '{method_name}' on expect()")
                return

            if method_name in ("to_have_count", "not_to_have_count"):
                for kw in node.keywords:
                    if kw.arg not in ("timeout",):
                        self.findings.append(
                            f"calls {method_name}() with invalid keyword argument '{kw.arg}' "
                            f"(only 'timeout' is allowed; do NOT use comparison arguments like greater_than, min, etc.)"
                        )


def lint_draft(code: str, *, web: bool, api: bool, event: bool = False, data: bool = False) -> list[str]:
    """Enforce best practices on a generated test. Returns human-readable violations.

    This is the 'guarantee, don't hope' check: drafts that violate it are regenerated once
    with the findings, then quarantined out of the runnable suite if still non-conforming.
    Web/API checks are gated on those surfaces; EVENT/DATA add their own contract-test checks."""
    findings: list[str] = []
    if any(b in code for b in _BANNED):
        findings.append("uses a hard sleep — rely on Playwright auto-waiting / expect()")
    # `validate(` (jsonschema) and `raises` (pytest.raises) are assertion forms too — an event
    # contract test may assert purely by a schema validate that raises on a non-conforming payload.
    if not any(tok in code for tok in ("assert", "expect(", "raises", "validate(")):
        findings.append("has no assertions")
    if "expect(" in code and "import expect" not in code:
        findings.append("uses expect() but never imports it (from playwright.sync_api import expect)")

    if "expect(" in code:
        try:
            tree = ast.parse(code)
            visitor = PlaywrightAssertionVisitor()
            visitor.visit(tree)
            findings.extend(visitor.findings)
        except SyntaxError:
            pass

    if web:
        imported, used = _page_object_usage(code)
        if not used:
            findings.append("web flow must drive a Page Object (import AND use one)")
        unused = sorted(imported - used)
        if unused:
            findings.append(
                f"remove unused Page Object import(s): {', '.join(unused)} — import only what the test uses"
            )
        if "expect(" not in code:
            findings.append("web flow must assert with Playwright's web-first expect()")
    if api and not web:
        if "api_request_context" not in code and "ApiClient" not in code:
            findings.append("API flow must use the api_request_context fixture")
    if event:
        if "message_schema" not in code and "jsonschema" not in code:
            findings.append(
                "event contract test must validate against a discovered schema "
                "(use the message_schema fixture and jsonschema.validate)"
            )
    if data:
        if "db_inspector" not in code:
            findings.append("database contract test must introspect via the db_inspector fixture")
    return findings


def _corrective(findings: list[str]) -> str:
    bullets = "\n".join(f"- {f}" for f in findings)
    return (
        "\n\nYour previous draft violated these REQUIREMENTS. Regenerate the COMPLETE module, "
        f"fixing every one:\n{bullets}\n"
    )


def run_test_file(test_file_path: Path, cwd: Path) -> tuple[int, str]:
    """Runs a single test file using pytest and captures the output.
    Returns (exit_code, output)."""
    import subprocess
    import shutil

    # Build command: use 'uv run pytest' if uv is available, else just 'pytest'
    cmd = ["pytest", "-ra", "--tb=short", str(test_file_path.relative_to(cwd))]
    if shutil.which("uv"):
        cmd = ["uv", "run"] + cmd

    try:
        res = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,  # 30 second timeout per test file
        )
        return res.returncode, res.stdout + res.stderr
    except subprocess.TimeoutExpired as e:
        stdout = e.stdout or ""
        stderr = e.stderr or ""
        return -1, f"Test execution timed out after 30 seconds:\n{stdout}\n{stderr}"
    except Exception as e:
        return -1, f"Failed to execute pytest: {e}"


def _runtime_corrective(output: str, *, prior_code: str | None = None) -> str:
    snippet = "\n".join(output.splitlines()[-40:])
    msg = (
        "\n\nYour previous draft failed at runtime. Regenerate the COMPLETE module, fixing the failure. "
    )
    if prior_code:
        msg += f"Here is the draft that failed (fix it, don't start over):\n```python\n{prior_code}\n```\n"
    msg += f"Here is the pytest failure output:\n```\n{snippet}\n```\n"
    return msg


def _runtime_failure_note(output: str, prior: str) -> str:
    """Fold a runtime failure trace into the draft's review notes as a single line.
    `_header` flattens newlines, so keep the trace on one line with ` | ` separators."""
    trace = " | ".join(line.strip() for line in output.splitlines()[-20:] if line.strip())
    return (
        "RUNTIME FAILURE (still failing after one self-heal). "
        f"Last pytest output: {trace}  ||  {prior}"
    )


async def _regenerate_and_validate(
    inv: CoverageInventory,
    journey: Journey,
    provider: LLMProvider,
    *,
    prompt: str,
    system_prompt: str,
    web: bool,
    api: bool,
    stem: str,
    label: str,
    output: str,
    prior_code: str,
    event: bool = False,
    data: bool = False,
) -> tuple[TestDraft, str, str, bool]:
    """One corrective regeneration from a runtime failure (the failing code + pytest output
    are fed back). Returns the new (draft, body, header) and whether it is `clean` — i.e.
    passes lint AND compiles. Non-destructive only; callers never heal skip-guarded flows."""
    draft = await provider.generate_structured(
        prompt + _runtime_corrective(output, prior_code=prior_code),
        TestDraft, system=system_prompt, label=label,
    )
    body = draft.code.strip() + "\n"
    header = _header(inv, journey, draft, destructive=False)
    clean = not lint_draft(draft.code, web=web, api=api, event=event, data=data) and _compiles(
        header + body, f"{stem}.py"
    )
    return draft, body, header, clean


async def _verify_and_heal(
    inv: CoverageInventory,
    journey: Journey,
    provider: LLMProvider,
    *,
    draft: TestDraft,
    body: str,
    header: str,
    prompt: str,
    system_prompt: str,
    stem: str,
    path: Path,
    into: Path,
    web: bool,
    api: bool,
    event: bool = False,
    data: bool = False,
) -> tuple[TestDraft, str, str, bool]:
    """Run the just-written test once; on failure attempt ONE self-healing retry, and
    return the (possibly updated) draft/body/header plus whether it is still failing.

    Only called for non-destructive journeys (mutating flows are skip-guarded, never run).
    The returned draft is ALWAYS lint-clean and compiling: a retry that regresses is
    discarded in favour of the original (which already passed lint + compile), so self-heal
    can never cost us a runnable draft. If the test still fails after the retry, the runtime
    trace is folded into review_notes so the still-runnable draft isn't trusted blindly."""
    rc, output = run_test_file(path, into)
    if rc == 0:
        return draft, body, header, False

    retry, retry_body, retry_header, clean = await _regenerate_and_validate(
        inv, journey, provider, prompt=prompt, system_prompt=system_prompt,
        web=web, api=api, event=event, data=data, stem=stem, label=f"write:{stem}",
        output=output, prior_code=draft.code,
    )
    # Adopt the retry only if it is itself clean & runnable; otherwise keep the original.
    if clean:
        draft, body, header = retry, retry_body, retry_header
        path.write_text(header + body, encoding="utf-8")
        rc, output = run_test_file(path, into)

    if rc != 0:
        draft.review_notes = _runtime_failure_note(output, draft.review_notes)
        header = _header(inv, journey, draft, destructive=False)
        path.write_text(header + body, encoding="utf-8")

    return draft, body, header, rc != 0


def _existing_flow_keys(tests_dir: Path) -> dict[str, Path]:
    """Map each already-drafted flow fingerprint (the `# Flow:` header stamp) to its file, so
    a flow the LLM merely renamed isn't re-drafted as a duplicate on the next discover."""
    out: dict[str, Path] = {}
    if not tests_dir.is_dir():
        return out
    for p in sorted(tests_dir.glob("test_*.py")):
        try:
            head = p.read_text(encoding="utf-8").splitlines()[:8]
        except OSError:
            continue
        for line in head:
            if line.startswith("# Flow:"):
                out.setdefault(line.split(":", 1)[1].strip(), p)
                break
    return out


def _unique(stem: str, used: set[str]) -> str:
    candidate, n = stem, 2
    while candidate in used:
        candidate = f"{stem}_{n}"
        n += 1
    used.add(candidate)
    return candidate


async def draft_tests(
    inv: CoverageInventory,
    provider: LLMProvider,
    *,
    into: Path | str,
    max_journeys: int = MAX_JOURNEYS,
    verify: bool = False,
    force: bool = False,
    on_draft: Callable[[WriteResult], None] | None = None,
) -> WriteReport:
    """Draft one test per selected journey into `into`/tests. Returns a report of what was
    written / quarantined / skipped. `on_draft` is called after each draft (progress).

    Non-destructive by default: a journey whose test file already exists is SKIPPED (no LLM
    call, no overwrite) so re-running over an evolved system only drafts the new flows and
    leaves reviewed tests intact. Pass `force=True` to regenerate everything."""
    into = Path(into)
    tests_dir = into / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    review_dir = into / "drafts_needs_review"

    report = WriteReport()
    used: set[str] = set()
    # Flows already on disk, keyed by stable fingerprint — so a renamed-but-identical flow
    # is recognised and skipped rather than re-drafted under a new filename.
    existing_flows = {} if force else _existing_flow_keys(tests_dir)

    for journey in select_journeys(inv, max_journeys):
        destructive = is_destructive(inv, journey)
        els = _elements_for(inv, journey)
        web = any(e.kind in ("page", "form") for e in els)
        api = any(e.kind == "endpoint" for e in els)
        event = any(e.kind in ("topic", "event_schema") for e in els)
        data = any(e.kind in ("table", "migration") for e in els)
        # Compute the test name first so usage is attributed per test ("write:<test>").
        stem = _unique(f"test_{_func_name(journey.name)}", used)
        test_path = tests_dir / f"{stem}.py"
        if not force:
            # Skip a flow that's already drafted — matched by its STABLE fingerprint (so a
            # rename doesn't fool us) or by an exact filename (covers pre-fingerprint files).
            # Keeps re-discovery incremental and never clobbers a reviewed test.
            existing = existing_flows.get(journey_fingerprint(inv, journey)) or (
                test_path if test_path.exists() else None
            )
            if existing is not None:
                result = WriteResult(journey.name, existing, "existing", False, destructive)
                report.skipped.append(result)
                if on_draft is not None:
                    on_draft(result)
                continue
        prompt = build_prompt(inv, journey, destructive=destructive)

        system_prompt = _SYSTEM_PROMPT
        draft = await provider.generate_structured(
            prompt, TestDraft, system=system_prompt, label=f"write:{stem}"
        )
        findings = lint_draft(draft.code, web=web, api=api, event=event, data=data)
        if findings:
            # one corrective retry with the specific violations fed back to the model
            draft = await provider.generate_structured(
                prompt + _corrective(findings), TestDraft, system=system_prompt, label=f"write:{stem}"
            )
            findings = lint_draft(draft.code, web=web, api=api, event=event, data=data)

        # Backend contract drafts are read-only by construction; if the model nonetheless
        # emitted real mutation (DML/commit or a broker publish), guard it like any
        # destructive flow. Judged from the generated CODE, not the journey prose.
        if not destructive and (event or data) and _mutates_backend(draft.code):
            destructive = True

        # Inject the skip guard deterministically — never rely on the model to add it.
        guard = _SKIP_BLOCK if destructive else ""
        body = guard + draft.code.strip() + "\n"
        header = _header(inv, journey, draft, destructive)
        compiles = _compiles(header + body, f"{stem}.py")

        if compiles and not findings:
            path = tests_dir / f"{stem}.py"
            path.write_text(header + body, encoding="utf-8")

            runtime_failed = False
            if verify and not destructive:
                # Self-heal keeps the draft clean & runnable; it never re-quarantines.
                draft, body, header, runtime_failed = await _verify_and_heal(
                    inv, journey, provider,
                    draft=draft, body=body, header=header,
                    prompt=prompt, system_prompt=system_prompt, stem=stem,
                    path=path, into=into, web=web, api=api, event=event, data=data,
                )

            result = WriteResult(
                journey.name, path, draft.confidence, False, destructive, runtime_failed
            )
            report.written.append(result)
        else:
            # Enforce-or-quarantine: keep non-conforming drafts out of the runnable suite.
            review_dir.mkdir(parents=True, exist_ok=True)
            reasons = list(findings) + ([] if compiles else ["module does not parse"])
            notes = "# LINT — fix before enabling:\n" + "".join(f"#   - {r}\n" for r in reasons) + "\n"
            path_txt = review_dir / f"{stem}.py.txt"
            path_txt.write_text(header + notes + body, encoding="utf-8")
            result = WriteResult(journey.name, path_txt, draft.confidence, True, destructive)
            report.quarantined.append(result)

        if on_draft is not None:
            on_draft(result)

    return report


# --------------------------------------------------------------------------------------
# Fix: self-heal tests that fail when actually run (the interactive twin of --verify)
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class HealResult:
    journey: str
    path: Path
    fixed: bool
    reason: str = ""


@dataclass(slots=True)
class HealReport:
    fixed: list[HealResult] = field(default_factory=list)
    still_failing: list[HealResult] = field(default_factory=list)
    passed: list[Path] = field(default_factory=list)  # already green; left untouched


def _header_value(src: str, prefix: str) -> str | None:
    for line in src.splitlines()[:10]:
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return None


def _split_header(src: str) -> tuple[str, str]:
    """Split a drafted test into its leading comment/blank header and the code body, so a heal
    can swap the code while PRESERVING provenance — including the stable `# Flow` stamp that
    later re-discovers match against (regenerating the header would change it and break that)."""
    lines = src.splitlines(keepends=True)
    i = 0
    while i < len(lines) and (lines[i].lstrip().startswith("#") or not lines[i].strip()):
        i += 1
    return "".join(lines[:i]), "".join(lines[i:])


def _infer_web_api(src: str) -> tuple[bool, bool]:
    """Best-effort web/api classification from a test's source — used to ground a fix when the
    test can't be matched back to a current flow (e.g. the LLM regrouped journeys since)."""
    api = "api_request_context" in src
    web = "from pages import" in src or "playwright.sync_api import expect" in src
    if not web and not api:
        web = "page" in src
        api = not web
    return web, api


def _infer_event_data(src: str) -> tuple[bool, bool]:
    """Best-effort EVENT/DATA classification from a test's source, for the same fallback path."""
    event = "message_schema" in src or "jsonschema" in src
    data = "db_inspector" in src
    return event, data


async def heal_failing_tests(
    inv: CoverageInventory,
    provider: LLMProvider,
    *,
    into: Path | str,
    max_journeys: int = MAX_JOURNEYS,
    on_heal: Callable[[HealResult], None] | None = None,
) -> HealReport:
    """Run each drafted test in `into`/tests once and self-heal the ones that fail.

    The interactive twin of `draft_tests(..., verify=True)`: it operates on tests already on
    disk. Destructive/skip-guarded drafts are never run. Each failing test is traced back to a
    flow by its STABLE `# Flow` fingerprint (so a renamed/regrouped inventory doesn't strand
    it), falling back to filename and then to the failing test itself — a fix is ALWAYS
    attempted, never refused for "no matching flow". One corrective regeneration (failing code
    + pytest output fed back); a regressing retry is discarded (file left as-is). If it still
    fails, the trace is noted. `on_heal` fires once per *attempted* (failing) file."""
    into = Path(into)
    tests_dir = into / "tests"
    report = HealReport()
    if not tests_dir.is_dir():
        return report

    journeys = select_journeys(inv, max_journeys)
    by_flow = {journey_fingerprint(inv, j): j for j in journeys}
    by_stem = {f"test_{_func_name(j.name)}": j for j in journeys}

    for path in sorted(tests_dir.glob("test_*.py")):
        src = path.read_text(encoding="utf-8")
        if "mark.skip" in src:
            continue  # destructive / explicitly skipped — not meant to run unattended
        rc, output = await asyncio.to_thread(run_test_file, path, into)
        if rc == 0:
            report.passed.append(path)
            continue

        # Match by stable flow fingerprint first (survives renames), then filename. If neither
        # resolves, ground the fix on the failing test itself — its code has the real paths.
        flow_id = _header_value(src, "# Flow:")
        journey = (by_flow.get(flow_id) if flow_id else None) or by_stem.get(path.stem)
        if journey is not None:
            els = _elements_for(inv, journey)
            web = any(e.kind in ("page", "form") for e in els)
            api = any(e.kind == "endpoint" for e in els)
            event = any(e.kind in ("topic", "event_schema") for e in els)
            data = any(e.kind in ("table", "migration") for e in els)
        else:
            web, api = _infer_web_api(src)
            event, data = _infer_event_data(src)
            name = (_header_value(src, "# Journey:") or path.stem).split("—")[0].strip()
            journey = Journey(
                name=name, description="(re-grounded from the failing test for a fix)",
                priority="medium", elements=[],
            )

        draft, body, _gen_header, clean = await _regenerate_and_validate(
            inv, journey, provider,
            prompt=build_prompt(inv, journey),
            system_prompt=_SYSTEM_PROMPT,
            web=web, api=api, event=event, data=data, stem=path.stem, label=f"fix:{path.stem}",
            output=output, prior_code=src,
        )
        if not clean:
            # The retry regressed (won't lint/compile). Keep the existing runnable file as-is.
            result = HealResult(journey.name, path, fixed=False, reason="self-heal produced invalid code")
            report.still_failing.append(result)
            if on_heal is not None:
                on_heal(result)
            continue

        # Preserve the original provenance header (incl. the # Flow stamp); swap only the code.
        orig_header, _ = _split_header(src)
        path.write_text(orig_header + body, encoding="utf-8")
        rc2, output2 = await asyncio.to_thread(run_test_file, path, into)
        if rc2 == 0:
            result = HealResult(journey.name, path, fixed=True)
            report.fixed.append(result)
        else:
            trace = " | ".join(ln.strip() for ln in output2.splitlines()[-15:] if ln.strip())
            note = f"# RUNTIME FAILURE (still failing after self-heal): {trace}\n"
            path.write_text(note + orig_header + body, encoding="utf-8")
            result = HealResult(journey.name, path, fixed=False, reason="still failing after self-heal")
            report.still_failing.append(result)

        if on_heal is not None:
            on_heal(result)

    return report


# --------------------------------------------------------------------------------------
# Enable: lift the destructive-skip guard so a reviewed draft runs ("skipped" -> "ok")
# --------------------------------------------------------------------------------------
#
# draft_tests stamps a deterministic skip guard onto destructive (mutating) drafts so a
# generated DELETE never runs by accident. Once a human has reviewed the draft and added
# teardown, this lifts that exact guard — the inverse of the injection. Deterministic, no
# LLM, and matched to OUR block so a `pytest.mark.skip` a human added by hand is untouched.


@dataclass(slots=True)
class EnableResult:
    path: Path
    enabled: bool
    reason: str = ""  # why it wasn't enabled (already runnable, no such file, ...)


def is_skip_guarded(src: str) -> bool:
    """True if `src` carries the destructive-skip guard this toolkit injects."""
    return _SKIP_BLOCK in src


def enable_draft_source(src: str) -> tuple[str, bool]:
    """Strip the injected destructive-skip guard from a draft's source so it runs.

    Returns (new_source, changed). Idempotent: source without our guard comes back unchanged.
    Only the exact block `draft_tests` stamped is removed, so a `pytest.mark.skip` a human
    added for their own reasons is left intact. If that guard was the body's only `import
    pytest` yet the test still uses pytest, the import is preserved so the module still
    compiles. The provenance header's 'skipped by default' note is rewritten to record that
    the skip was deliberately lifted."""
    if _SKIP_BLOCK not in src:
        return src, False
    out = src.replace(_SKIP_BLOCK, "", 1)
    if "pytest." in out and "import pytest" not in out:
        out = "import pytest\n\n" + out
    out = out.replace(
        "| DESTRUCTIVE: skipped by default",
        "| DESTRUCTIVE: enabled (skip lifted — verify teardown)",
    )
    return out, True


def find_skipped_drafts(into: Path | str) -> list[Path]:
    """Drafted test files under `into`/tests that still carry the injected skip guard."""
    tests_dir = Path(into) / "tests"
    if not tests_dir.is_dir():
        return []
    out: list[Path] = []
    for p in sorted(tests_dir.glob("test_*.py")):
        try:
            if is_skip_guarded(p.read_text(encoding="utf-8")):
                out.append(p)
        except OSError:
            continue
    return out


def _resolve_target(tests_dir: Path, target: str) -> Path | None:
    """Resolve a user-supplied test name to a file. Accepts 'foo', 'test_foo', or
    'test_foo.py' (with or without the test_ prefix / .py suffix)."""
    stem = target[:-3] if target.endswith(".py") else target
    for cand in (tests_dir / f"{stem}.py", tests_dir / f"test_{stem}.py"):
        if cand.is_file():
            return cand
    return None


def _enable_one(path: Path) -> EnableResult:
    new, changed = enable_draft_source(path.read_text(encoding="utf-8"))
    if not changed:
        return EnableResult(path, False, "not skip-guarded (already runnable)")
    path.write_text(new, encoding="utf-8")
    return EnableResult(path, True)


def enable_drafts(into: Path | str, *, targets: list[str] | None = None) -> list[EnableResult]:
    """Lift the destructive-skip guard on drafted tests under `into`/tests.

    `targets` selects specific tests by name; when None, ALL skip-guarded drafts are enabled.
    Returns one result per file acted on (or per unresolved target), so the caller can report
    exactly what changed. Enabling a destructive test means it will perform mutating requests
    when run — callers should surface that."""
    tests_dir = Path(into) / "tests"
    results: list[EnableResult] = []
    if not tests_dir.is_dir():
        return results

    if targets:
        for t in targets:
            path = _resolve_target(tests_dir, t)
            results.append(
                _enable_one(path) if path is not None
                else EnableResult(tests_dir / t, False, "no such test file")
            )
    else:
        for path in find_skipped_drafts(into):
            results.append(_enable_one(path))
    return results

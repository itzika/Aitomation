"""Tests for the deterministic scaffold stage. No LLM, no network — pure templating."""

from __future__ import annotations

import pytest

from aitomation.models import AuthScheme, CoverageInventory, InputField, Journey, JourneyStep
from aitomation.models import TestableElement as Element  # aliased: avoid pytest "Test*" collection
from aitomation.scaffold import inventory_to_context, scaffold_project
from aitomation.scaffold.generator import _class_name, _func_name, _normalize_auth, _slug


def _api_inventory(auth: str | None = "bearer") -> CoverageInventory:
    return CoverageInventory(
        system_name="Demo API",
        base_url="https://api.demo",
        source="openapi",
        auth_strategy=auth,
        elements=[
            Element(
                kind="endpoint",
                name="create_thing",
                location="/things",
                method="POST",
                description='Create a "thing"',
                priority="high",
            ),
            Element(
                kind="endpoint",
                name="get_thing",
                location="/things/{id}",
                method="GET",
                description="Read",
                priority="medium",
            ),
            Element(
                kind="auth", name="bearer auth", location="/login", description="x", priority="high"
            ),
        ],
        suggested_journeys=[
            Journey(
                name="Create then read",
                description="round trip",
                priority="high",
                steps=[JourneyStep(action="create"), JourneyStep(action="read")],
            )
        ],
    )


def _web_inventory() -> CoverageInventory:
    return CoverageInventory(
        system_name="Demo Web 2",
        base_url="https://web.demo",
        source="crawl",
        auth_strategy="session",
        elements=[
            Element(kind="page", name="Home", location="/", description="Home", priority="medium"),
            Element(
                kind="form",
                name="Login Form",
                location="/login",
                description="Login",
                priority="high",
                inputs=[
                    InputField(name="username", where="form", required=True),
                    InputField(name="password", where="form", required=True, type="password"),
                ],
            ),
        ],
    )


# --- naming + auth helpers ---------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("oauth2", "bearer"),
        ("apiKey", "bearer"),
        ("Bearer token", "bearer"),
        ("basic", "basic"),
        ("session", "session"),
        ("cookie-based", "session"),
        (None, "none"),
        ("none", "none"),
    ],
)
def test_normalize_auth(raw, expected):
    assert _normalize_auth(raw) == expected


def test_naming_helpers():
    assert _class_name("get all") == "GetAllPage"
    assert _class_name("Login Form") == "LoginFormPage"
    assert _func_name("List Pets!") == "list_pets"
    assert _func_name("123 go") == "f_123_go"
    assert _slug("Rick and Morty API") == "rick_and_morty_api"


# --- context -----------------------------------------------------------------------------


def test_context_api():
    ctx = inventory_to_context(_api_inventory())
    assert ctx["has_api"] is True and ctx["has_browser"] is False
    assert len(ctx["endpoints"]) == 2
    assert ctx["auth_strategy"] == "bearer"
    assert ctx["package_slug"] == "demo_api"
    # smoke path skips the {id} endpoint in favour of a plain one (relative, no leading slash)
    assert ctx["smoke_path"] == "things"
    # quotes in descriptions are neutralised so generated docstrings stay valid
    assert '"' not in ctx["endpoints"][0]["description"]
    # request paths are base-relative so a base path (e.g. /api) isn't dropped by Playwright
    assert all(not e["rel_path"].startswith("/") for e in ctx["endpoints"])


def test_context_web():
    ctx = inventory_to_context(_web_inventory())
    assert ctx["has_browser"] is True
    classes = {p["class_name"] for p in ctx["pages"]}
    assert classes == {"HomePage", "LoginFormPage"}
    assert ctx["auth_strategy"] == "session"


def test_auth_context_prefers_apikey_header_over_oauth2():
    inv = _api_inventory()
    # spec with both, like Petstore — the easy-to-supply apiKey-in-header should win
    inv.auth_schemes = [
        AuthScheme(type="oauth2"),
        AuthScheme(type="apiKey", name="api_key", location="header"),
    ]
    ctx = inventory_to_context(inv)
    assert ctx["auth_kind"] == "apikey"
    assert ctx["auth_header_name"] == "api_key" and ctx["auth_in"] == "header"


def test_auth_context_basic_and_bearer():
    inv = _api_inventory()
    inv.auth_schemes = [AuthScheme(type="http", scheme="basic")]
    assert inventory_to_context(inv)["auth_kind"] == "basic"
    inv.auth_schemes = [AuthScheme(type="http", scheme="bearer")]
    assert inventory_to_context(inv)["auth_kind"] == "bearer"


def test_apikey_scaffold_emits_correct_header(tmp_path):
    inv = _api_inventory(auth="apiKey")
    inv.auth_schemes = [AuthScheme(type="apiKey", name="api_key", location="header")]
    dest = scaffold_project(inv, tmp_path / "e2e")
    conftest = (dest / "conftest.py").read_text()
    _compiles(dest / "conftest.py")  # valid Python, no leftover Jinja
    assert '{"api_key": key}' in conftest  # the actual discovered header name
    assert "Bearer" not in conftest  # NOT collapsed to a bearer token


def test_apikey_in_query_does_not_fake_a_header(tmp_path):
    inv = _api_inventory(auth="apiKey")
    inv.auth_schemes = [AuthScheme(type="apiKey", name="token", location="query")]
    dest = scaffold_project(inv, tmp_path / "e2e")
    conftest = (dest / "conftest.py").read_text()
    _compiles(dest / "conftest.py")
    # query-key can't ride in extra_http_headers; fixture stays empty with a note
    assert "params=" in conftest and "Bearer" not in conftest


# --- generation --------------------------------------------------------------------------


def _compiles(path) -> None:
    src = path.read_text(encoding="utf-8")
    assert "{%" not in src and "{{" not in src, f"leftover Jinja in {path.name}"
    compile(src, str(path), "exec")  # raises SyntaxError on bad output


def test_scaffold_api_project(tmp_path):
    dest = scaffold_project(_api_inventory(), tmp_path / "e2e")
    for name in ("conftest.py", "api_client.py", "tests/test_smoke.py", "reporting.py", "pages.py"):
        _compiles(dest / name)

    assert (dest / "pyproject.toml").exists()
    assert (dest / "Dockerfile").exists()
    assert (dest / ".github/workflows/e2e.yml").exists()
    assert (dest / ".env.example").exists()
    # pro pytest defaults: trace/screenshot on failure + registered markers
    pyproject = (dest / "pyproject.toml").read_text()
    assert "--tracing=retain-on-failure" in pyproject and "markers" in pyproject

    conftest = (dest / "conftest.py").read_text()
    assert "Bearer" in conftest and "AUTH_TOKEN" in conftest
    # base_url normalised with a trailing slash so relative request paths resolve correctly
    assert 'rstrip("/") + "/"' in conftest

    client = (dest / "api_client.py").read_text()
    assert "def create_thing(" in client and "def get_thing(" in client
    assert 'self.request.post("things"' in client  # relative path (no leading slash)

    smoke = (dest / "tests/test_smoke.py").read_text()
    assert 'api_request_context.get("things")' in smoke
    assert "test_home_loads" not in smoke  # API-only: no browser test


def test_scaffold_web_project(tmp_path):
    dest = scaffold_project(_web_inventory(), tmp_path / "e2e")
    for name in ("conftest.py", "pages.py", "tests/test_smoke.py"):
        _compiles(dest / name)

    pages = (dest / "pages.py").read_text()
    assert "class HomePage:" in pages and "class LoginFormPage:" in pages
    # form fields seeded as role/label locators (POM isn't empty)
    assert 'self.username = page.get_by_label("username")' in pages
    assert "def fill(self, **values: str)" in pages
    # Page Objects must not be collected by pytest (e.g. a "Test…"-named page)
    assert "__test__ = False" in pages

    conftest = (dest / "conftest.py").read_text()
    # session auth uses storage_state (log in once, reuse) — the Playwright best practice
    assert "storage_state" in conftest and "browser_context_args" in conftest

    smoke = (dest / "tests/test_smoke.py").read_text()
    assert "def test_home_loads(" in smoke


def test_page_object_uses_observed_locator_and_first(tmp_path):
    inv = CoverageInventory(
        system_name="Shop",
        base_url="https://shop.demo",
        source="crawl",
        elements=[
            Element(
                kind="form",
                name="login_form",
                location="/login",
                description="login",
                priority="high",
                inputs=[
                    InputField(
                        name="email",
                        where="form",
                        required=True,
                        locator='get_by_placeholder("Email Address")',
                        unique=False,
                    )
                ],
            ),
        ],
    )
    dest = scaffold_project(inv, tmp_path / "e2e")
    pages = (dest / "pages.py").read_text()
    _compiles(dest / "pages.py")
    # uses the observed locator, with .first because it wasn't unique on the page
    assert 'self.email = page.get_by_placeholder("Email Address").first' in pages


def test_base_url_trailing_slash_is_normalized():
    inv = _web_inventory()
    inv.base_url = "https://web.demo/"  # trailing slash → would cause f"{base_url}/path" → //path
    ctx = inventory_to_context(inv)
    assert ctx["base_url"] == "https://web.demo"


def test_scaffold_is_deterministic(tmp_path):
    inv = _api_inventory()
    a = scaffold_project(inv, tmp_path / "a")
    b = scaffold_project(inv, tmp_path / "b")
    fa = (a / "api_client.py").read_text()
    fb = (b / "api_client.py").read_text()
    assert fa == fb


# --- backend surfaces (events + databases) -----------------------------------------------


def _event_inventory() -> CoverageInventory:
    return CoverageInventory(
        system_name="Orders Events",
        base_url="kafka://broker",
        source="asyncapi",
        elements=[
            Element(
                kind="topic",
                name="orderCreated",
                location="orders.created",
                method="receive",
                description="Order placed.",
                priority="high",
            ),
            Element(
                kind="event_schema",
                name="OrderCreated",
                location="orders.created",
                description="Order created event.",
                priority="high",
                inputs=[InputField(name="orderId", type="string", required=True, where="message")],
                json_schema={
                    "type": "object",
                    "required": ["orderId"],
                    "properties": {"orderId": {"type": "string"}},
                },
            ),
        ],
    )


def _db_inventory() -> CoverageInventory:
    return CoverageInventory(
        system_name="shop (sqlite schema)",
        base_url="sqlite:///shop.db",
        source="db_schema",
        elements=[
            Element(
                kind="table",
                name="users",
                location="users",
                description="Users table.",
                priority="high",
                inputs=[
                    InputField(name="id", type="INTEGER", required=True, where="column"),
                    InputField(name="email", type="TEXT", required=True, where="column"),
                ],
                preconditions=["PRIMARY KEY (id)", "UNIQUE (email)"],
            ),
        ],
    )


def test_context_backend_flags_and_lists():
    ev = inventory_to_context(_event_inventory())
    assert ev["has_events"] is True and ev["has_db"] is False
    assert {t["name"] for t in ev["topics"]} == {"orderCreated"}
    assert ev["event_schemas"][0]["has_schema"] is True

    db = inventory_to_context(_db_inventory())
    assert db["has_db"] is True and db["has_events"] is False
    cols = {c["name"] for c in db["tables"][0]["columns"]}
    assert cols == {"id", "email"}


def test_scaffold_event_project(tmp_path):
    dest = scaffold_project(_event_inventory(), tmp_path / "e2e")
    _compiles(dest / "conftest.py")
    _compiles(dest / "tests/test_smoke.py")

    conftest = (dest / "conftest.py").read_text()
    assert "def message_schema(" in conftest and "schemas.json" in conftest
    # event-only: no browser/api fixtures leaked in
    assert "api_request_context" not in conftest

    # the discovered schema is emitted verbatim for jsonschema.validate(...)
    import json

    schemas = json.loads((dest / "schemas.json").read_text())
    assert schemas["OrderCreated"]["required"] == ["orderId"]

    assert "jsonschema>=4" in (dest / "pyproject.toml").read_text()
    assert "test_message_schemas_load" in (dest / "tests/test_smoke.py").read_text()


def test_scaffold_db_project(tmp_path):
    dest = scaffold_project(_db_inventory(), tmp_path / "e2e")
    _compiles(dest / "conftest.py")
    _compiles(dest / "tests/test_smoke.py")

    conftest = (dest / "conftest.py").read_text()
    assert "def db_inspector(" in conftest and "DATABASE_URL" in conftest
    assert "sqlalchemy>=2" in (dest / "pyproject.toml").read_text()
    assert "DATABASE_URL=" in (dest / ".env.example").read_text()
    assert "test_database_reachable" in (dest / "tests/test_smoke.py").read_text()
    # no schemas.json when there are no event schemas
    assert not (dest / "schemas.json").exists()


def test_pure_backend_scaffold_has_no_placeholder(tmp_path):
    dest = scaffold_project(_db_inventory(), tmp_path / "e2e")
    smoke = (dest / "tests/test_smoke.py").read_text()
    # backend smoke present means the empty placeholder must NOT be emitted
    assert "test_placeholder" not in smoke

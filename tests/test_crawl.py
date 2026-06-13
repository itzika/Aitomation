"""Tests for the live-crawl path.

Pure URL logic is tested directly. The crawler itself is exercised end-to-end against an
in-process HTTP server (no network, fully deterministic). Browser tests skip cleanly if
Chromium isn't installed so the suite stays runnable everywhere.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from aitomation.discover.crawl import (
    CrawlResult,
    Form,
    FormField,
    PageArtifact,
    _field_locator,
    crawl_site,
    elements_from_crawl,
    normalize_links,
    render_crawl,
    same_origin,
)

# --------------------------------------------------------------------------------------
# Pure helpers — no browser
# --------------------------------------------------------------------------------------


def test_same_origin():
    assert same_origin("http://a.com/x", "http://a.com/y")
    assert not same_origin("http://a.com", "https://a.com")  # scheme differs
    assert not same_origin("http://a.com", "http://b.com")


def test_normalize_links_resolves_filters_and_dedupes():
    base = "https://shop.example/catalog"
    hrefs = [
        "/cart",  # relative -> same origin
        "item?id=1#reviews",  # fragment stripped, relative resolved
        "https://shop.example/cart",  # dupe of /cart
        "https://other.com/x",  # cross-origin dropped
        "mailto:hi@example.com",  # non-http dropped
        "#top",  # pure fragment dropped
        None,
        "",
    ]
    links = normalize_links(base, hrefs)
    # urljoin replaces the last path segment ("catalog"), so item resolves at the root.
    assert links == [
        "https://shop.example/cart",
        "https://shop.example/item?id=1",
    ]


def test_field_locator_priority():
    # test-id (data-qa/testid) wins — most stable + unique
    assert (
        _field_locator({"testId": "name", "testAttr": "data-qa"})[0]
        == "locator('[data-qa=\"name\"]')"
    )
    # then a real <label>, then placeholder, then aria-label, then the name attribute
    assert _field_locator({"labelText": "Your Name"})[0] == 'get_by_label("Your Name")'
    assert (
        _field_locator({"placeholder": "Email Address"})[0] == 'get_by_placeholder("Email Address")'
    )
    assert _field_locator({"ariaLabel": "Search"})[0] == 'get_by_label("Search")'
    assert _field_locator({"name": "email"})[0] == "locator('[name=\"email\"]')"


def test_elements_from_crawl_grounds_locators_and_uniqueness():
    # two forms each with an "Email Address" placeholder field → ambiguous (unique=False)
    def email(unique):
        return FormField(
            name="email",
            type="email",
            human="Email Address",
            locator='get_by_placeholder("Email Address")',
            unique=unique,
        )

    login = Form(
        action="/login",
        method="post",
        has_password=True,
        fields=[
            email(False),
            FormField(
                name="password",
                type="password",
                human="password",
                locator="locator('[name=\"password\"]')",
                unique=True,
            ),
        ],
    )
    news = Form(action="/news", method="post", fields=[email(False)])
    page = PageArtifact(url="https://x/login", title="Login", depth=0, forms=[login, news])
    elements = elements_from_crawl(CrawlResult(base_url="https://x", pages=[page]))

    assert [e.kind for e in elements].count("form") == 2
    assert any(e.kind == "page" for e in elements)
    login_el = next(e for e in elements if e.kind == "form" and e.preconditions)
    assert "requires authenticated session" in login_el.preconditions  # has_password → login
    assert login_el.method == "POST"  # POST form → is_destructive() will skip it by default
    email_input = next(i for i in login_el.inputs if i.name == "email")
    assert email_input.locator == 'get_by_placeholder("Email Address")'
    assert email_input.unique is False  # carried through for the scaffold to add .first


def test_render_crawl_marks_login_forms():
    result = CrawlResult(
        base_url="http://x",
        pages=[
            PageArtifact(
                url="http://x/login",
                title="Login",
                depth=1,
                forms=[],
            )
        ],
    )
    text = render_crawl(result)
    assert "/login" in text
    assert "Pages discovered: 1" in text


# --------------------------------------------------------------------------------------
# Integration — real crawler against an in-process site
# --------------------------------------------------------------------------------------

_PAGES = {
    "/": b"""<html><head><title>Home</title></head><body>
        <nav><a href="/about">About</a> <a href="/login">Login</a>
        <a href="https://external.example/x">External</a></nav>
        <button>Get started</button></body></html>""",
    "/about": b"<html><head><title>About</title></head><body><h1>About us</h1></body></html>",
    "/login": b"""<html><head><title>Login</title></head><body>
        <form action="/session" method="post">
          <input name="username" type="text" required aria-label="Username"/>
          <input name="password" type="password" required/>
          <button type="submit">Sign in</button>
        </form></body></html>""",
}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = _PAGES.get(self.path.split("?")[0])
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence test server logging
        pass


@pytest.fixture(scope="module")
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()


@pytest.fixture(scope="module")
def chromium_ready():
    """Skip browser tests cleanly when Chromium isn't installed."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            browser.close()
    except Exception as e:
        pytest.skip(f"Chromium unavailable: {e}")


async def test_crawl_site_discovers_pages_forms_and_links(site, chromium_ready):
    result = await crawl_site(site, max_pages=10, max_depth=2)

    routes = {p.url.replace(site, "") or "/" for p in result.pages}
    assert {"/", "/about", "/login"} <= routes

    login = next(p for p in result.pages if p.url.endswith("/login"))
    assert len(login.forms) == 1
    form = login.forms[0]
    assert form.has_password is True
    assert form.method == "post"
    field_names = {f.name for f in form.fields}
    assert {"username", "password"} <= field_names
    assert all(f.locator for f in form.fields)  # every field got a grounded locator

    home = next(p for p in result.pages if p.url.rstrip("/") == site)
    # external link must not be followed / recorded as in-app
    assert all("external.example" not in link for link in home.links)
    assert any(b for b in home.buttons)


async def test_crawl_respects_max_pages(site, chromium_ready):
    result = await crawl_site(site, max_pages=1, max_depth=3)
    assert len(result.pages) == 1


# -- first-run self-heal: browser binaries missing ----------------------------------------


class _FakeChromium:
    def __init__(self, failures: list[Exception]) -> None:
        self.failures = failures
        self.launches = 0

    async def launch(self, **kwargs):
        self.launches += 1
        if self.failures:
            raise self.failures.pop(0)
        return "browser"


class _FakePW:
    def __init__(self, failures: list[Exception]) -> None:
        self.chromium = _FakeChromium(failures)


async def test_launch_installs_chromium_once_then_retries(monkeypatch):
    from aitomation.discover import crawl as crawl_mod

    installed = []
    monkeypatch.setattr(crawl_mod, "_install_chromium", lambda: installed.append(True))
    pw = _FakePW(
        [
            Exception(
                'BrowserType.launch: Executable doesn\'t exist at /x — run "playwright install"'
            )
        ]
    )
    statuses: list[str] = []

    browser = await crawl_mod._launch_chromium(pw, statuses.append)
    assert browser == "browser"
    assert installed == [True]  # auto-install ran exactly once
    assert pw.chromium.launches == 2  # failed launch + retry
    assert any("downloading" in s.lower() for s in statuses)  # announced, not a silent hang


async def test_launch_reraises_non_setup_errors(monkeypatch):
    from aitomation.discover import crawl as crawl_mod

    monkeypatch.setattr(
        crawl_mod, "_install_chromium", lambda: pytest.fail("must not install on unrelated errors")
    )
    pw = _FakePW([Exception("net::ERR_CONNECTION_REFUSED")])
    with pytest.raises(Exception, match="CONNECTION_REFUSED"):
        await crawl_mod._launch_chromium(pw, None)


async def test_failed_install_raises_actionable_error(monkeypatch):
    from aitomation.discover import crawl as crawl_mod

    def _boom():
        raise RuntimeError(
            "Chromium auto-install failed: no network. Install it manually "
            "with `uv run playwright install chromium`, then retry."
        )

    monkeypatch.setattr(crawl_mod, "_install_chromium", _boom)
    pw = _FakePW([Exception("Executable doesn't exist at /x")])
    with pytest.raises(RuntimeError, match="playwright install chromium"):
        await crawl_mod._launch_chromium(pw, None)

"""Live-crawl discovery path.

Architectural choice: this is a *deterministic* Playwright crawl, not an agent driving the
Playwright MCP. It mirrors the OpenAPI path — bounded, reproducible extraction of crawl
artifacts (routes, accessibility tree, forms, links) feeding a single LLM call that
produces the validated CoverageInventory. The a11y tree is the signal (not pixels), per
the spec. Auth-walled crawling and SPA deep-interaction are deliberately out of this MLP
slice; detecting login forms is in scope, automating login is not.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlsplit

from ..models import CoverageInventory, InputField, TestableElement
from ..providers import LLMProvider
from .openapi import InventoryJudgment, render_elements_for_prompt

MAX_PAGES = 25
MAX_DEPTH = 3
MAX_LINKS_PER_PAGE = 40
MAX_ARIA_LINES = 45
NAV_TIMEOUT_MS = 15_000


# --------------------------------------------------------------------------------------
# Artifacts
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class FormField:
    name: str
    type: str = "text"
    required: bool = False
    human: str = ""  # human-readable identity (label/placeholder/test-id)
    locator: str | None = None  # observed Playwright locator expression
    unique: bool = True  # False if this locator matches >1 field on the page


@dataclass(slots=True)
class Form:
    action: str
    method: str
    fields: list[FormField] = field(default_factory=list)
    has_password: bool = False  # strong signal this is a login/auth form


@dataclass(slots=True)
class PageArtifact:
    url: str
    title: str
    depth: int
    aria: str = ""  # YAML-like accessibility-tree snapshot (roles/names/urls), bounded
    forms: list[Form] = field(default_factory=list)
    buttons: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass(slots=True)
class CrawlResult:
    base_url: str
    pages: list[PageArtifact] = field(default_factory=list)


# --------------------------------------------------------------------------------------
# Pure helpers (unit-testable without a browser)
# --------------------------------------------------------------------------------------


def same_origin(a: str, b: str) -> bool:
    sa, sb = urlsplit(a), urlsplit(b)
    return (sa.scheme, sa.netloc) == (sb.scheme, sb.netloc)


def normalize_links(base_url: str, hrefs: list[str | None]) -> list[str]:
    """Resolve hrefs against base_url, keep same-origin http(s), drop fragments, dedupe."""
    seen: set[str] = set()
    out: list[str] = []
    for href in hrefs:
        if not href:
            continue
        href = href.strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#", "data:")):
            continue
        absolute, _ = urldefrag(urljoin(base_url, href))
        if not absolute.startswith(("http://", "https://")):
            continue
        if not same_origin(base_url, absolute):
            continue
        if absolute not in seen:
            seen.add(absolute)
            out.append(absolute)
    return out


def _route(url: str) -> str:
    parts = urlsplit(url)
    return parts.path or "/"


# --------------------------------------------------------------------------------------
# Browser extraction
# --------------------------------------------------------------------------------------

_FORMS_JS = """
() => Array.from(document.querySelectorAll('form')).map(f => ({
  action: f.getAttribute('action') || '',
  method: (f.getAttribute('method') || 'get').toLowerCase(),
  fields: Array.from(f.querySelectorAll('input, select, textarea'))
    .filter(i => (i.getAttribute('type') || '') !== 'hidden')
    .map(i => {
      const labelEl = i.labels && i.labels[0];
      const testAttr = i.hasAttribute('data-qa') ? 'data-qa'
        : (i.hasAttribute('data-testid') ? 'data-testid'
        : (i.hasAttribute('data-test') ? 'data-test' : ''));
      return {
        name: i.getAttribute('name') || i.getAttribute('id') || '',
        type: (i.getAttribute('type') || i.tagName).toLowerCase(),
        required: i.hasAttribute('required'),
        placeholder: (i.getAttribute('placeholder') || '').trim(),
        ariaLabel: (i.getAttribute('aria-label') || '').trim(),
        labelText: labelEl ? labelEl.textContent.trim() : '',
        testAttr: testAttr,
        testId: testAttr ? i.getAttribute(testAttr) : ''
      };
    })
}))
"""


def _field_locator(raw: dict) -> tuple[str | None, str]:
    """Choose the most robust Playwright locator for a field, preferring stable test-id
    attributes, then a real <label>, then placeholder/aria-label, then the name attribute.
    Returns (locator_expression, human_identity)."""

    def q(s: str) -> str:  # avoid quote-escaping headaches in generated code
        return s.replace('"', "'")

    if raw.get("testId"):
        return f"locator('[{raw['testAttr']}=\"{raw['testId']}\"]')", raw["testId"]
    if raw.get("labelText"):
        return f'get_by_label("{q(raw["labelText"])}")', raw["labelText"]
    if raw.get("placeholder"):
        return f'get_by_placeholder("{q(raw["placeholder"])}")', raw["placeholder"]
    if raw.get("ariaLabel"):
        return f'get_by_label("{q(raw["ariaLabel"])}")', raw["ariaLabel"]
    if raw.get("name"):
        return f"locator('[name=\"{raw['name']}\"]')", raw["name"]
    return None, "field"


_BUTTONS_JS = """
() => Array.from(document.querySelectorAll('button, [role=button], input[type=submit]'))
  .map(b => (b.innerText || b.value || b.getAttribute('aria-label') || '').trim())
  .filter(Boolean)
"""

_LINKS_JS = (
    "() => Array.from(document.querySelectorAll('a[href]')).map(a => a.getAttribute('href'))"
)


def _bound_aria(snapshot: str) -> str:
    """Keep the a11y snapshot prompt-sized: drop noisy /url lines, cap line count."""
    lines = [ln for ln in snapshot.splitlines() if ln.strip() and "/url:" not in ln]
    if len(lines) > MAX_ARIA_LINES:
        lines = [*lines[:MAX_ARIA_LINES], "  - …(truncated)"]
    return "\n".join(lines)


async def extract_page(page, depth: int) -> PageArtifact:  # page: playwright Page
    """Extract a single page's testable surface. Resilient: never raises on bad content."""
    url = page.url
    artifact = PageArtifact(url=url, title=await page.title(), depth=depth)

    try:
        artifact.aria = _bound_aria(await page.aria_snapshot())

        raw_forms = await page.evaluate(_FORMS_JS)
        # Compute each field's locator, then mark non-unique ones (page-wide) so tests/
        # page-objects can add `.first` instead of hitting Playwright strict-mode errors.
        for rf in raw_forms:
            for raw in rf["fields"]:
                raw["_loc"], raw["_human"] = _field_locator(raw)
        loc_counts = Counter(raw["_loc"] for rf in raw_forms for raw in rf["fields"] if raw["_loc"])
        for rf in raw_forms:
            fields = []
            for raw in rf["fields"]:
                if not (raw["name"] or raw["_human"] != "field"):
                    continue
                fields.append(
                    FormField(
                        name=raw["name"] or raw["_human"],
                        type=raw["type"],
                        required=raw["required"],
                        human=raw["_human"],
                        locator=raw["_loc"],
                        unique=loc_counts.get(raw["_loc"], 0) <= 1,
                    )
                )
            artifact.forms.append(
                Form(
                    action=rf["action"],
                    method=rf["method"],
                    fields=fields,
                    has_password=any(f.type == "password" for f in fields),
                )
            )

        artifact.buttons = (await page.evaluate(_BUTTONS_JS))[:20]
        hrefs = await page.evaluate(_LINKS_JS)
        artifact.links = normalize_links(url, hrefs)[:MAX_LINKS_PER_PAGE]
    except Exception as e:
        artifact.error = f"{type(e).__name__}: {e}"
    return artifact


async def crawl_site(
    base_url: str,
    *,
    max_pages: int = MAX_PAGES,
    max_depth: int = MAX_DEPTH,
    on_page: Callable[[PageArtifact], None] | None = None,
) -> CrawlResult:
    """Bounded, same-origin BFS crawl. Returns deterministic artifacts for the LLM.

    `on_page`, if given, is called after each page is crawled (for live progress)."""
    from playwright.async_api import async_playwright

    result = CrawlResult(base_url=base_url)
    queue: list[tuple[str, int]] = [(base_url, 0)]
    visited: set[str] = set()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()
        page.set_default_navigation_timeout(NAV_TIMEOUT_MS)

        while queue and len(result.pages) < max_pages:
            url, depth = queue.pop(0)
            norm, _ = urldefrag(url)
            if norm in visited:
                continue
            visited.add(norm)

            try:
                await page.goto(url, wait_until="domcontentloaded")
            except Exception as e:
                broken = PageArtifact(
                    url=url, title="", depth=depth, error=f"{type(e).__name__}: {e}"
                )
                result.pages.append(broken)
                if on_page is not None:
                    on_page(broken)
                continue

            artifact = await extract_page(page, depth)
            result.pages.append(artifact)
            if on_page is not None:
                on_page(artifact)

            if depth < max_depth:
                for link in artifact.links:
                    ln, _ = urldefrag(link)
                    if ln not in visited:
                        queue.append((link, depth + 1))

        await browser.close()
    return result


# --------------------------------------------------------------------------------------
# Rendering + LLM
# --------------------------------------------------------------------------------------


def render_crawl(result: CrawlResult) -> str:
    lines = [f"Crawled site: {result.base_url}", f"Pages discovered: {len(result.pages)}", ""]
    for p in result.pages:
        lines.append(f"## {_route(p.url)}  ({p.url})")
        if p.title:
            lines.append(f"   title: {p.title}")
        if p.error:
            lines.append(f"   error: {p.error}")
            continue
        if p.aria:
            indented = "\n".join("     " + ln for ln in p.aria.splitlines())
            lines.append("   accessibility tree:\n" + indented)
        for form in p.forms:
            tag = " [LOGIN-LIKE]" if form.has_password else ""
            fields = ", ".join(
                f"{fld.name}{'*' if fld.required else ''}:{fld.type}" for fld in form.fields
            )
            lines.append(
                f"   form{tag}: {form.method.upper()} {form.action or '(self)'} — {fields}"
            )
        if p.buttons:
            lines.append("   buttons: " + ", ".join(p.buttons[:15]))
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Deterministic element enumeration (the grounding: pages/forms + REAL locators come from
# the crawl, never the model). The LLM only supplies judgement over this fixed surface.
# --------------------------------------------------------------------------------------


def _name_from_route(route: str) -> str:
    slug = re.sub(r"[^0-9a-zA-Z]+", "_", route).strip("_").lower()
    return f"{slug}_page" if slug else "home_page"


def _unique(name: str, seen: set[str]) -> str:
    candidate, n = name, 2
    while candidate in seen:
        candidate, n = f"{name}_{n}", n + 1
    seen.add(candidate)
    return candidate


def elements_from_crawl(result: CrawlResult) -> list[TestableElement]:
    """Build the page/form elements deterministically, carrying each field's observed
    locator + uniqueness so downstream stages never have to guess selectors."""
    elements: list[TestableElement] = []
    seen: set[str] = set()
    for p in result.pages:
        if p.error:
            continue
        route = _route(p.url)
        page_name = _unique(_name_from_route(route), seen)
        elements.append(
            TestableElement(
                kind="page",
                name=page_name,
                location=route,
                description=p.title or route,
                priority="medium",
            )
        )
        for idx, form in enumerate(p.forms):
            base = f"{page_name}_form" if len(p.forms) == 1 else f"{page_name}_form_{idx + 1}"
            inputs = [
                InputField(
                    name=f.name,
                    type=f.type,
                    required=f.required,
                    where="form",
                    description=f.human or None,
                    locator=f.locator,
                    unique=f.unique,
                )
                for f in form.fields
            ]
            elements.append(
                TestableElement(
                    kind="form",
                    name=_unique(base, seen),
                    location=route,
                    # carry the form's HTTP method so a POST form is correctly treated as
                    # destructive (skipped by default); a GET search form is not.
                    method=(form.method or "get").upper(),
                    description=f"Form on {route}" + (" (login)" if form.has_password else ""),
                    inputs=inputs,
                    preconditions=["requires authenticated session"] if form.has_password else [],
                    priority="high" if form.has_password else "medium",
                )
            )
    return elements


_CRAWL_JUDGMENT_SYSTEM = """\
You are a senior test-automation analyst. You are given the COMPLETE, authoritative list of
testable elements (pages and forms) discovered by crawling a web app's accessibility tree.
The list is exhaustive and correct: you must NOT add, remove, rename, or invent elements,
routes, or fields.

Your job is judgement only:
- Prioritise by exception: list the HIGH-priority element names and the LOW-priority element
  names. Everything you DON'T list is treated as medium, so only name the ones that genuinely
  stand out. Auth/login and state-changing forms are usually high; static pages are usually
  low. Copy names EXACTLY; do not relist mediums.
- Infer auth_strategy: 'session' if a login form is present, otherwise null.
- Write a concise system summary.
- Propose 5-10 realistic end-to-end journeys chaining pages/forms (e.g. land -> navigate ->
  submit a form). Reference element names EXACTLY as given.
"""


def build_crawl_judgment_prompt(result: CrawlResult, elements: list[TestableElement]) -> str:
    return (
        f"Crawled web app: {result.base_url}\n"
        f"Testable elements ({len(elements)}) — complete and authoritative:\n"
        f"{render_elements_for_prompt(elements)}\n\n"
        "Provide the HIGH-priority and LOW-priority element names (by exact name; omit mediums), "
        "the auth_strategy, a system summary, and 5-10 suggested journeys referencing these "
        "element names."
    )


async def discover_crawl(
    base_url: str,
    provider: LLMProvider,
    *,
    max_pages: int = MAX_PAGES,
    max_depth: int = MAX_DEPTH,
    on_page: Callable[[PageArtifact], None] | None = None,
) -> CoverageInventory:
    """Live-crawl discovery: crawl -> enumerate elements (with real locators) deterministically
    -> LLM supplies judgement (priorities/auth/journeys) -> merged inventory."""
    result = await crawl_site(base_url, max_pages=max_pages, max_depth=max_depth, on_page=on_page)
    if not any(p.error is None for p in result.pages):
        raise ValueError(f"Crawl of {base_url} yielded no usable pages.")

    elements = elements_from_crawl(result)
    names = {e.name for e in elements}

    judgment = await provider.generate_structured(
        build_crawl_judgment_prompt(result, elements),
        InventoryJudgment,
        system=_CRAWL_JUDGMENT_SYSTEM,
        label="discover.crawl",
    )

    priorities = judgment.priority_map(names)
    for el in elements:
        el.priority = priorities.get(el.name, el.priority)
    for journey in judgment.suggested_journeys:
        journey.elements = [n for n in journey.elements if n in names]

    home = next((p for p in result.pages if not p.error and _route(p.url) in ("/", "")), None)
    system_name = (
        (home.title if home and home.title else "") or urlsplit(base_url).netloc or base_url
    )
    return CoverageInventory(
        system_name=system_name,
        base_url=base_url,
        source="crawl",
        auth_strategy=judgment.auth_strategy,
        summary=judgment.system_summary,
        elements=elements,
        suggested_journeys=judgment.suggested_journeys,
    )

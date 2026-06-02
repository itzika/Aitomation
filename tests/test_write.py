"""Tests for the Write stage. The LLM is stubbed — we test selection, grounding of the
prompt, file routing (written vs quarantined), and the human-authoritative header."""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

from aitomation.models import CoverageInventory, InputField, Journey, JourneyStep
from aitomation.models import TestableElement as Element  # aliased: avoid pytest "Test*" collection
from aitomation.write import (
    draft_tests,
    enable_drafts,
    find_skipped_drafts,
    is_destructive,
    select_journeys,
)
from aitomation.write.generator import (
    _SKIP_BLOCK,
    _SYSTEM_PROMPT,
    build_prompt,
    build_system_prompt,
    enable_draft_source,
    journey_type,
    lint_draft,
)

T = TypeVar("T", bound=BaseModel)


def _inv() -> CoverageInventory:
    return CoverageInventory(
        system_name="Demo API",
        base_url="https://api.demo",
        source="openapi",
        auth_strategy=None,
        elements=[
            Element(kind="endpoint", name="list_things", location="/things", method="GET",
                    description="List", priority="medium"),
            Element(kind="endpoint", name="get_thing", location="/things/{id}", method="GET",
                    description="Read", priority="high"),
        ],
        suggested_journeys=[
            Journey(name="Low one", description="low", priority="low",
                    steps=[JourneyStep(action="x")], elements=["list_things"]),
            Journey(name="High one", description="hi", priority="high",
                    steps=[JourneyStep(action="list"), JourneyStep(action="get")],
                    elements=["list_things", "get_thing"]),
            Journey(name="Mid one", description="mid", priority="medium",
                    steps=[JourneyStep(action="y")], elements=["get_thing"]),
        ],
    )


class _FakeProvider:
    """Returns a fixed draft; optionally returns un-parseable code to test quarantine.
    Pass `codes=[...]` to return a different draft per call (clamped to the last) — used to
    simulate a self-heal retry that produces better or worse code than the original."""

    def __init__(
        self,
        code: str = "def test_ok(api_request_context):\n    assert True\n",
        codes: list[str] | None = None,
    ) -> None:
        self.code = code
        self.codes = list(codes) if codes is not None else None
        self.prompts: list[str] = []
        self.labels: list[str] = []

    async def generate(self, prompt: str, *, system: str | None = None, label: str = "") -> str:  # pragma: no cover
        return ""

    async def generate_structured(self, prompt, schema: type[T], *, system=None, label: str = "") -> T:
        self.prompts.append(prompt)
        self.labels.append(label)
        if self.codes is not None:
            code = self.codes[min(len(self.prompts) - 1, len(self.codes) - 1)]
        else:
            code = self.code
        return schema(code=code, confidence="medium", review_notes="check ids")


def test_select_journeys_orders_by_priority():
    js = select_journeys(_inv())
    assert [j.name for j in js] == ["High one", "Mid one", "Low one"]


def test_select_journeys_caps_count():
    assert len(select_journeys(_inv(), max_journeys=2)) == 2


def test_select_journeys_synthesizes_when_none():
    inv = _inv()
    inv.suggested_journeys = []
    js = select_journeys(inv, max_journeys=5)
    # high-priority element first
    assert js and js[0].elements == ["get_thing"]


def test_build_prompt_is_grounded():
    inv = _inv()
    high = next(j for j in inv.suggested_journeys if j.name == "High one")
    prompt = build_prompt(inv, high)
    assert "https://api.demo" in prompt
    assert "GET /things/{id}" in prompt  # only elements the journey touches, with real paths
    assert "1. list" in prompt and "2. get" in prompt


async def test_draft_tests_writes_compiling_files(tmp_path):
    provider = _FakeProvider()
    report = await draft_tests(_inv(), provider, into=tmp_path, max_journeys=2)

    assert len(report.written) == 2 and not report.quarantined
    # highest-priority journey drafted first, into tests/
    first = report.written[0]
    assert first.journey == "High one"
    assert first.path.parent.name == "tests"

    body = first.path.read_text()
    assert body.startswith("# AI FIRST-DRAFT")  # human-authoritative provenance header
    assert "Journey: High one" in body
    assert "def test_ok" in body
    compile(body, str(first.path), "exec")  # really runnable


async def test_draft_tests_quarantines_unparseable(tmp_path):
    provider = _FakeProvider(code="def broken(:\n  oops")
    report = await draft_tests(_inv(), provider, into=tmp_path, max_journeys=1)

    assert not report.written and len(report.quarantined) == 1
    q = report.quarantined[0]
    assert q.needs_review is True
    assert q.path.suffix == ".txt"  # kept out of tests/ so pytest collection stays green
    assert q.path.parent.name == "drafts_needs_review"


async def test_draft_tests_skips_existing_unless_forced(tmp_path):
    provider = _FakeProvider()
    r1 = await draft_tests(_inv(), provider, into=tmp_path, max_journeys=2)
    assert len(r1.written) == 2 and not r1.skipped
    after_first = len(provider.prompts)

    # second run over the same dir: files exist → all skipped, no new LLM calls, untouched
    first_path = r1.written[0].path
    original = first_path.read_text()
    r2 = await draft_tests(_inv(), provider, into=tmp_path, max_journeys=2)
    assert r2.written == [] and len(r2.skipped) == 2
    assert r2.skipped[0].confidence == "existing"
    assert len(provider.prompts) == after_first  # incremental: nothing regenerated
    assert first_path.read_text() == original

    # force regenerates everything
    r3 = await draft_tests(_inv(), provider, into=tmp_path, max_journeys=2, force=True)
    assert len(r3.written) == 2 and not r3.skipped
    assert len(provider.prompts) > after_first


async def test_draft_tests_skip_survives_journey_rename(tmp_path):
    # The LLM renames journeys on every discover; the SAME flow (same element set) must not
    # be re-drafted as a duplicate. Skip is matched by the stable flow fingerprint, not name.
    provider = _FakeProvider()
    r1 = await draft_tests(_inv(), provider, into=tmp_path, max_journeys=2)
    assert len(r1.written) == 2
    after_first = len(provider.prompts)

    renamed = _inv()  # identical elements/flows, but the journeys come back renamed
    renamed.suggested_journeys[1].name = "Completely Different High Flow Name"  # was "High one"
    renamed.suggested_journeys[2].name = "A Fresh Name For The Mid Flow"  # was "Mid one"

    r2 = await draft_tests(renamed, provider, into=tmp_path, max_journeys=2)
    assert r2.written == [] and len(r2.skipped) == 2  # recognised by element-set
    assert len(provider.prompts) == after_first  # nothing regenerated
    # the skip points at the original file, and no duplicate was created
    assert len(list((tmp_path / "tests").glob("test_*.py"))) == 2


def _mutating_inv() -> CoverageInventory:
    return CoverageInventory(
        system_name="Store", base_url="https://store.demo", source="openapi", auth_strategy="apiKey",
        elements=[
            Element(kind="endpoint", name="list_pets", location="/pets", method="GET",
                    description="List", priority="medium"),
            Element(kind="endpoint", name="create_pet", location="/pets", method="POST",
                    description="Create", priority="high"),
            Element(kind="endpoint", name="delete_pet", location="/pets/{id}", method="DELETE",
                    description="Delete", priority="high"),
        ],
        suggested_journeys=[
            Journey(name="Read pets", description="r", priority="high",
                    steps=[JourneyStep(action="list")], elements=["list_pets"]),
            Journey(name="Create and delete", description="crud", priority="high",
                    steps=[JourneyStep(action="create"), JourneyStep(action="delete")],
                    elements=["create_pet", "delete_pet"]),
        ],
    )


def test_is_destructive_web_forms_password_or_explicit_submit():
    inv = CoverageInventory(
        system_name="Web", base_url="https://web.demo", source="crawl",
        elements=[
            Element(kind="form", name="login_form", location="/login", method="POST",
                    description="login", priority="high",
                    inputs=[InputField(name="email", where="form"),
                            InputField(name="password", where="form", type="password")]),
            Element(kind="form", name="newsletter_form", location="/", method="POST",
                    description="newsletter", priority="medium",
                    inputs=[InputField(name="email", where="form")]),
            Element(kind="form", name="search_form", location="/products", method="GET",
                    description="search", priority="medium",
                    inputs=[InputField(name="q", where="form")]),
        ],
        suggested_journeys=[],
    )

    def j(name, elements, *steps):
        return Journey(name=name, description=name, priority="high",
                       steps=[JourneyStep(action=s) for s in steps], elements=elements)

    # password-bearing form → destructive regardless of verbs
    assert is_destructive(inv, j("login", ["login_form"], "go to login", "enter creds")) is True
    # incidental POST footer form (journey doesn't submit it) → NOT destructive
    assert is_destructive(inv, j("browse", ["newsletter_form"], "land on home", "read page")) is False
    # the same form, explicitly submitted → destructive
    assert is_destructive(inv, j("sub", ["newsletter_form"], "subscribe to newsletter")) is True
    # GET search form is never destructive, even with a submit verb
    assert is_destructive(inv, j("search", ["search_form"], "submit search query")) is False


def test_is_destructive_flags_mutating_journeys():
    inv = _mutating_inv()
    read = next(j for j in inv.suggested_journeys if j.name == "Read pets")
    crud = next(j for j in inv.suggested_journeys if j.name == "Create and delete")
    assert is_destructive(inv, read) is False
    assert is_destructive(inv, crud) is True


async def test_destructive_drafts_are_skipped_by_default(tmp_path):
    report = await draft_tests(_mutating_inv(), _FakeProvider(), into=tmp_path)
    by_journey = {r.journey: r for r in report.written}

    read = by_journey["Read pets"]
    crud = by_journey["Create and delete"]
    assert read.destructive is False
    assert crud.destructive is True

    # the read draft runs; the mutating one is emitted but skip-guarded
    read_src = read.path.read_text()
    crud_src = crud.path.read_text()
    assert "pytestmark = pytest.mark.skip" not in read_src
    assert "pytestmark = pytest.mark.skip" in crud_src
    assert "DESTRUCTIVE" in crud_src
    compile(crud_src, str(crud.path), "exec")  # still valid/importable


def test_enable_draft_source_lifts_guard_and_is_idempotent():
    src = (
        "# AI FIRST-DRAFT — review before trusting. Generated by Aitomation Write.\n"
        "# Source: Store (openapi) | confidence: high | DESTRUCTIVE: skipped by default\n\n\n"
        + _SKIP_BLOCK
        + "def test_create(api_request_context):\n    assert True\n"
    )
    out, changed = enable_draft_source(src)
    assert changed is True
    assert "pytestmark = pytest.mark.skip" not in out
    assert "DESTRUCTIVE: skipped by default" not in out
    assert "DESTRUCTIVE: enabled (skip lifted" in out
    compile(out, "test_create.py", "exec")  # still importable after the guard is removed

    # Idempotent: a source without our guard comes back unchanged.
    assert enable_draft_source(out) == (out, False)


def test_enable_draft_source_preserves_pytest_import_when_body_needs_it():
    # The guard carried the only `import pytest`, but the body still uses pytest → keep it.
    src = _SKIP_BLOCK + "def test_x(api_request_context):\n    with pytest.raises(ValueError):\n        pass\n"
    out, changed = enable_draft_source(src)
    assert changed and "pytestmark = pytest.mark.skip" not in out
    assert "import pytest" in out
    compile(out, "test_x.py", "exec")


async def test_enable_drafts_targets_all_and_unresolved(tmp_path):
    await draft_tests(_mutating_inv(), _FakeProvider(), into=tmp_path)

    # Only the mutating (crud) draft is skip-guarded; the read one is not.
    skipped = find_skipped_drafts(tmp_path)
    assert len(skipped) == 1
    target = skipped[0]

    # Enable by name (works with or without the test_ prefix / .py suffix).
    results = enable_drafts(tmp_path, targets=[target.stem])
    assert [r.enabled for r in results] == [True]
    assert "pytestmark = pytest.mark.skip" not in target.read_text()

    # Now nothing is skipped; re-enabling is a reported no-op, not an error.
    assert find_skipped_drafts(tmp_path) == []
    again = enable_drafts(tmp_path, targets=[target.stem])
    assert again[0].enabled is False and "already runnable" in again[0].reason

    # Unresolved target is reported, not raised.
    missing = enable_drafts(tmp_path, targets=["nope"])
    assert missing[0].enabled is False and "no such" in missing[0].reason


async def test_enable_drafts_all_enables_every_skipped(tmp_path):
    await draft_tests(_mutating_inv(), _FakeProvider(), into=tmp_path)
    assert len(find_skipped_drafts(tmp_path)) == 1
    results = enable_drafts(tmp_path)  # targets=None → enable all skipped
    assert sum(r.enabled for r in results) == 1
    assert find_skipped_drafts(tmp_path) == []


def test_system_prompt_has_data_discipline():
    # the #3 hardening: defensive-assertion guidance is part of the contract
    assert "(200, 404)" in _SYSTEM_PROMPT
    assert "non-empty" in _SYSTEM_PROMPT and "exactly equal" in _SYSTEM_PROMPT


def test_build_prompt_surfaces_input_examples():
    inv = CoverageInventory(
        system_name="X", base_url="https://x", source="openapi",
        elements=[
            Element(
                kind="endpoint", name="make", location="/things", method="POST",
                description="create a thing", priority="high",
                inputs=[InputField(name="title", where="body", required=True, example="Demo")],
            )
        ],
        suggested_journeys=[
            Journey(name="j", description="d", priority="high", steps=[], elements=["make"])
        ],
    )
    text = build_prompt(inv, inv.suggested_journeys[0])
    assert "title" in text and "Demo" in text


def test_destructive_prompt_asks_for_teardown():
    inv = _mutating_inv()
    crud = next(j for j in inv.suggested_journeys if j.name == "Create and delete")
    prompt = build_prompt(inv, crud, destructive=True)
    assert "MUTATING" in prompt and "CLEAN UP" in prompt


def test_lint_draft_rules():
    # API flow: must use the request fixture and have assertions
    good_api = "def test_a(api_request_context):\n    r = api_request_context.get('x')\n    assert r.ok\n"
    assert lint_draft(good_api, web=False, api=True) == []
    assert "api_request_context" in " ".join(lint_draft("def t():\n    assert 1", web=False, api=True))

    # web flow: must use a Page Object and Playwright expect()
    bad_web = "def test_w(page):\n    page.goto('/')\n    assert True\n"
    findings = lint_draft(bad_web, web=True, api=False)
    assert any("Page Object" in f for f in findings)
    assert any("expect()" in f for f in findings)
    good_web = ("from pages import HomePage\nfrom playwright.sync_api import expect\n"
                "def test_w(page):\n    home = HomePage(page).goto()\n    expect(page).to_have_title('x')\n")
    assert lint_draft(good_web, web=True, api=False) == []

    # importing a Page Object but never using it (the Goodhart hole) must be caught
    imported_unused = ("from pages import HomePage\nfrom playwright.sync_api import expect\n"
                       "def test_w(page):\n    page.goto('/')\n    expect(page).to_have_title('x')\n")
    assert any("unused" in f for f in lint_draft(imported_unused, web=True, api=False))

    # importing TWO but using only ONE must flag the unused one by name
    two_one = ("from pages import HomePage, BrandPage\nfrom playwright.sync_api import expect\n"
               "def test_w(page):\n    BrandPage(page).goto()\n    expect(page).to_have_title('x')\n")
    f = lint_draft(two_one, web=True, api=False)
    assert any("unused" in x and "HomePage" in x for x in f)
    assert not any("BrandPage" in x for x in f)  # the used one isn't flagged

    # using expect() without importing it is a (runtime) bug the lint must catch
    expect_no_import = ("from pages import HomePage\ndef test_w(page):\n"
                        "    HomePage(page).goto()\n    expect(page).to_have_title('x')\n")
    assert any("imports it" in f for f in lint_draft(expect_no_import, web=True, api=False))

    # hard sleeps are banned for any flow
    assert any("sleep" in f for f in lint_draft("import time\ntime.sleep(1)\nassert 1", web=False, api=True))


def _web_inv() -> CoverageInventory:
    return CoverageInventory(
        system_name="Web", base_url="https://web.demo", source="crawl",
        elements=[Element(kind="page", name="Home", location="/", description="home", priority="high")],
        suggested_journeys=[Journey(name="Visit home", description="d", priority="high",
                                    steps=[JourneyStep(action="open")], elements=["Home"])],
    )


async def test_nonconforming_web_draft_is_quarantined(tmp_path):
    # the fake returns API-style code that violates the web POM/expect rules; after one
    # corrective retry it still doesn't conform, so it must be quarantined, not written.
    provider = _FakeProvider(code="def test_ok(api_request_context):\n    assert True\n")
    report = await draft_tests(_web_inv(), provider, into=tmp_path, max_journeys=1)
    assert report.written == [] and len(report.quarantined) == 1
    q = report.quarantined[0]
    assert q.path.suffix == ".txt" and q.needs_review is True
    body = q.path.read_text()
    assert "LINT" in body and "Page Object" in body
    # the corrective retry means two model calls for this one draft
    assert provider.prompts and any("REQUIREMENTS" in p for p in provider.prompts)


async def test_draft_filenames_are_unique(tmp_path):
    # two journeys whose names slugify identically must not collide
    inv = _inv()
    inv.suggested_journeys = [
        Journey(name="Do thing", description="a", priority="high", steps=[]),
        Journey(name="Do thing!", description="b", priority="high", steps=[]),
    ]
    report = await draft_tests(inv, _FakeProvider(), into=tmp_path)
    names = {r.path.name for r in report.written}
    assert names == {"test_do_thing.py", "test_do_thing_2.py"}


def test_lint_draft_catches_invalid_have_count():
    code = (
        "from playwright.sync_api import expect\n"
        "from pages import HomePage\n"
        "def test_x(page):\n"
        "    HomePage(page).goto()\n"
        "    expect(page.locator('.item')).to_have_count(greater_than=0)\n"
    )
    findings = lint_draft(code, web=True, api=False)
    assert any("greater_than" in f for f in findings)


def test_lint_draft_catches_invalid_assertion_method():
    code = (
        "from playwright.sync_api import expect\n"
        "from pages import HomePage\n"
        "def test_x(page):\n"
        "    HomePage(page).goto()\n"
        "    expect(page.locator('.item')).to_have_status(200)\n"
    )
    findings = lint_draft(code, web=True, api=False)
    assert any("to_have_status" in f for f in findings)


def test_system_prompt_is_static_and_complete():
    # One invariant prompt (no args) so the cached prefix is reused across every write/fix
    # call. It must carry ALL rule sets — web, API, and both — gated by the journey type the
    # user prompt declares, rather than being conditionally trimmed per call.
    prompt = build_system_prompt()
    assert prompt == _SYSTEM_PROMPT  # the module constant IS the static prompt
    assert "For WEB+API or WEB-only journeys:" in prompt
    assert "For WEB+API or API-only journeys:" in prompt
    assert "For API-only journeys (no web surface):" in prompt
    assert "For WEB-only journeys (no API surface):" in prompt
    # All the load-bearing guidance survives in the single prompt.
    assert "Use the generated Page Objects" in prompt
    assert "Use the `api_request_context` fixture" in prompt
    assert "Playwright Assertions Reference Guide" in prompt


def test_build_prompt_declares_journey_type():
    inv = _inv()
    # API-only inventory (_inv builds endpoints) → user prompt must state the type so the
    # static system prompt's conditional rules resolve.
    journey = inv.suggested_journeys[0]
    assert "Journey type: API-only" in build_prompt(inv, journey)
    assert journey_type(web=True, api=True) == "WEB+API"
    assert journey_type(web=False, api=True) == "API-only"
    assert journey_type(web=True, api=False) == "WEB-only"


def test_draft_tests_verify_self_heals_failures(tmp_path):
    from unittest.mock import patch, MagicMock
    import asyncio

    # Code that compiles and passes lint rules
    ok_code = (
        "from pages import HomePage\n"
        "from playwright.sync_api import expect\n"
        "def test_w(page):\n"
        "    HomePage(page).goto()\n"
        "    expect(page).to_have_title('x')\n"
    )
    provider = _FakeProvider(code=ok_code)

    # 1st run: fails with AssertionError, 2nd run: passes
    mock_run = MagicMock(side_effect=[(1, "AssertionError: expected 'x' but got 'y'"), (0, "Success")])

    with patch("aitomation.write.generator.run_test_file", mock_run):
        report = asyncio.run(draft_tests(_web_inv(), provider, into=tmp_path, max_journeys=1, verify=True))

        assert mock_run.call_count == 2
        assert len(report.written) == 1
        assert not report.quarantined

        assert len(provider.prompts) == 2
        assert "AssertionError: expected 'x' but got 'y'" in provider.prompts[1]


_WEB_OK = (
    "from pages import HomePage\n"
    "from playwright.sync_api import expect\n"
    "def test_w(page):\n"
    "    HomePage(page).goto()\n"
    "    expect(page).to_have_title('x')\n"
)


def test_draft_tests_verify_passing_test_no_retry(tmp_path):
    from unittest.mock import patch, MagicMock
    import asyncio

    provider = _FakeProvider(code=_WEB_OK)
    mock_run = MagicMock(side_effect=[(0, "1 passed")])

    with patch("aitomation.write.generator.run_test_file", mock_run):
        report = asyncio.run(draft_tests(_web_inv(), provider, into=tmp_path, max_journeys=1, verify=True))

    # passes first time → no self-heal, only the initial generation call
    assert mock_run.call_count == 1
    assert len(provider.prompts) == 1
    assert len(report.written) == 1 and not report.quarantined
    assert report.written[0].runtime_failed is False
    assert "RUNTIME FAILURE" not in report.written[0].path.read_text()


def test_draft_tests_verify_still_failing_kept_runnable_with_note(tmp_path):
    from unittest.mock import patch, MagicMock
    import asyncio

    provider = _FakeProvider(code=_WEB_OK)
    # fails, retry is adopted (clean code), still fails the second run
    mock_run = MagicMock(side_effect=[(1, "first trace"), (1, "TimeoutError: locator not found")])

    with patch("aitomation.write.generator.run_test_file", mock_run):
        report = asyncio.run(draft_tests(_web_inv(), provider, into=tmp_path, max_journeys=1, verify=True))

    # syntactically valid & lint-clean → stays runnable, NOT quarantined
    assert len(report.written) == 1 and not report.quarantined
    assert report.written[0].runtime_failed is True
    assert mock_run.call_count == 2 and len(provider.prompts) == 2
    body = report.written[0].path.read_text()
    assert body.startswith("# AI FIRST-DRAFT")
    assert "RUNTIME FAILURE" in body and "TimeoutError: locator not found" in body
    compile(body, str(report.written[0].path), "exec")


def test_draft_tests_verify_discards_regressing_retry(tmp_path):
    from unittest.mock import patch, MagicMock
    import asyncio

    # 1st draft is clean web code; the self-heal retry regresses into API-style code that
    # violates the web lint rules. The regressing retry must be discarded — the original
    # stays in tests/ (runnable) with the runtime failure folded into its notes.
    regressed = "def test_ok(api_request_context):\n    assert True\n"
    provider = _FakeProvider(codes=[_WEB_OK, regressed])
    mock_run = MagicMock(side_effect=[(1, "AssertionError: boom")])

    with patch("aitomation.write.generator.run_test_file", mock_run):
        report = asyncio.run(draft_tests(_web_inv(), provider, into=tmp_path, max_journeys=1, verify=True))

    assert len(report.written) == 1 and not report.quarantined
    assert report.written[0].runtime_failed is True
    # retry was generated (2 prompts) but never adopted, so the test is only run once
    assert len(provider.prompts) == 2 and mock_run.call_count == 1
    body = report.written[0].path.read_text()
    assert "HomePage" in body and "api_request_context" not in body  # original kept, not the regression
    assert "RUNTIME FAILURE" in body and "AssertionError: boom" in body
    compile(body, str(report.written[0].path), "exec")


# -- heal_failing_tests (the interactive "f" fix) ---------------------------------------


def _write_test_file(into, stem: str, content: str):
    d = into / "tests"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{stem}.py"
    p.write_text(content, encoding="utf-8")
    return p


def test_heal_fixes_a_failing_test(tmp_path):
    from unittest.mock import patch, MagicMock
    import asyncio
    from aitomation.write import heal_failing_tests

    p = _write_test_file(tmp_path, "test_visit_home", "def test_w(page):\n    assert False\n")
    provider = _FakeProvider(code=_WEB_OK)
    # initial run fails, the healed rewrite passes
    mock_run = MagicMock(side_effect=[(1, "first failure trace"), (0, "1 passed")])

    with patch("aitomation.write.generator.run_test_file", mock_run):
        report = asyncio.run(heal_failing_tests(_web_inv(), provider, into=tmp_path))

    assert len(report.fixed) == 1 and not report.still_failing and not report.passed
    assert mock_run.call_count == 2 and len(provider.prompts) == 1
    # the regeneration is grounded in BOTH the failing code and the pytest output
    assert "first failure trace" in provider.prompts[0]
    assert "assert False" in provider.prompts[0]
    assert "HomePage" in p.read_text()  # file replaced with the healed draft


def test_heal_leaves_passing_tests_untouched(tmp_path):
    from unittest.mock import patch, MagicMock
    import asyncio
    from aitomation.write import heal_failing_tests

    original = "def test_w(page):\n    assert True\n"
    p = _write_test_file(tmp_path, "test_visit_home", original)
    provider = _FakeProvider(code=_WEB_OK)
    mock_run = MagicMock(side_effect=[(0, "1 passed")])

    with patch("aitomation.write.generator.run_test_file", mock_run):
        report = asyncio.run(heal_failing_tests(_web_inv(), provider, into=tmp_path))

    assert report.passed == [p] and not report.fixed and not report.still_failing
    assert provider.prompts == []  # no LLM call for a passing test
    assert p.read_text() == original


def test_heal_still_failing_annotates_notes(tmp_path):
    from unittest.mock import patch, MagicMock
    import asyncio
    from aitomation.write import heal_failing_tests

    p = _write_test_file(tmp_path, "test_visit_home", "def test_w(page):\n    assert False\n")
    provider = _FakeProvider(code=_WEB_OK)
    mock_run = MagicMock(side_effect=[(1, "trace one"), (1, "TimeoutError: boom")])

    with patch("aitomation.write.generator.run_test_file", mock_run):
        report = asyncio.run(heal_failing_tests(_web_inv(), provider, into=tmp_path))

    assert len(report.still_failing) == 1 and not report.fixed
    assert report.still_failing[0].reason == "still failing after self-heal"
    body = p.read_text()
    assert "RUNTIME FAILURE" in body and "TimeoutError: boom" in body
    compile(body, str(p), "exec")  # still runnable


def test_heal_discards_regressing_retry(tmp_path):
    from unittest.mock import patch, MagicMock
    import asyncio
    from aitomation.write import heal_failing_tests

    original = _WEB_OK
    p = _write_test_file(tmp_path, "test_visit_home", original)
    # the retry regresses into API-style code that violates the web lint rules
    provider = _FakeProvider(code="def test_ok(api_request_context):\n    assert True\n")
    mock_run = MagicMock(side_effect=[(1, "trace")])  # only the initial run; retry not adopted

    with patch("aitomation.write.generator.run_test_file", mock_run):
        report = asyncio.run(heal_failing_tests(_web_inv(), provider, into=tmp_path))

    assert len(report.still_failing) == 1 and not report.fixed
    assert report.still_failing[0].reason == "self-heal produced invalid code"
    assert mock_run.call_count == 1 and len(provider.prompts) == 1
    assert p.read_text() == original  # untouched — never replaced with worse code


def test_heal_skips_destructive_and_skipped(tmp_path):
    from unittest.mock import patch, MagicMock
    import asyncio
    from aitomation.write import heal_failing_tests

    skip_src = (
        "import pytest\n"
        "pytestmark = pytest.mark.skip(reason='DESTRUCTIVE journey')\n"
        "def test_w(page):\n    assert True\n"
    )
    _write_test_file(tmp_path, "test_visit_home", skip_src)
    provider = _FakeProvider(code=_WEB_OK)
    mock_run = MagicMock()

    with patch("aitomation.write.generator.run_test_file", mock_run):
        report = asyncio.run(heal_failing_tests(_web_inv(), provider, into=tmp_path))

    # skip-guarded tests are never run or healed
    assert not report.fixed and not report.still_failing and not report.passed
    assert mock_run.call_count == 0 and provider.prompts == []


def test_heal_handles_renamed_journeys(tmp_path):
    from unittest.mock import patch, MagicMock
    import asyncio
    from aitomation.write import heal_failing_tests

    provider = _FakeProvider(code=_WEB_OK)
    # draft so the test file carries the stable `# Flow` stamp in its header
    asyncio.run(draft_tests(_web_inv(), provider, into=tmp_path, max_journeys=1))

    inv_v2 = _web_inv()
    inv_v2.suggested_journeys[0].name = "A Completely Different Flow Name"  # LLM renamed it

    mock_run = MagicMock(side_effect=[(1, "AssertionError: boom"), (0, "ok")])
    with patch("aitomation.write.generator.run_test_file", mock_run):
        report = asyncio.run(heal_failing_tests(inv_v2, provider, into=tmp_path))

    # matched by the stable flow fingerprint despite the rename → healed, never "no matching flow"
    assert len(report.fixed) == 1 and not report.still_failing


def test_heal_grounds_from_failing_test_when_flow_unmatched(tmp_path):
    from unittest.mock import patch, MagicMock
    import asyncio
    from aitomation.write import heal_failing_tests

    # a test whose flow id + filename match nothing in the current inventory (regrouped/removed)
    _write_test_file(
        tmp_path, "test_orphan",
        "# AI FIRST-DRAFT\n# Journey: Orphan Flow — gone\n# Flow: deadbeef00\n\n\n"
        "def test_w(page):\n    assert False\n",
    )
    provider = _FakeProvider(code=_WEB_OK)
    mock_run = MagicMock(side_effect=[(1, "boom"), (0, "ok")])
    with patch("aitomation.write.generator.run_test_file", mock_run):
        report = asyncio.run(heal_failing_tests(_web_inv(), provider, into=tmp_path))

    # grounded from the failing test itself and healed — NOT refused as "no matching flow"
    assert len(report.fixed) == 1 and not report.still_failing
    body = (tmp_path / "tests" / "test_orphan.py").read_text()
    assert "# Flow: deadbeef00" in body  # original provenance preserved through the heal



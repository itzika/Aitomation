"""Headless tests of the Workbench TUI via Textual's pilot.

Exercises the non-LLM structural flows — browsing the library, selecting a system,
populating tabs, scaffolding, and the onboarding wizard — without network or a model.
Discovery/write themselves are covered by their own module tests."""

from __future__ import annotations

from pathlib import Path

from textual.widgets import DataTable, Input, RadioButton, RadioSet

from aitomation.models import CoverageInventory, Journey
from aitomation.models import TestableElement as Element  # aliased: avoid pytest "Test*" collection
from aitomation.tui import AitomationApp, Workspace
from aitomation.tui.app import ConfirmScreen, HelpScreen, ModelScreen, WizardScreen

_LLM_ENV = (
    "AITOMATION_PROVIDER", "AITOMATION_MODEL", "AITOMATION_API_KEY", "AITOMATION_BASE_URL",
    "AITOMATION_OUTPUT_MODE", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DASHSCOPE_API_KEY",
)


class _FakeLLM:
    async def generate(self, *a, **k) -> str:  # pragma: no cover
        return ""

    async def generate_structured(self, *a, **k):  # pragma: no cover
        raise NotImplementedError


def _seed(root: Path) -> None:
    inv = CoverageInventory(
        system_name="Demo API", base_url="https://api.demo", source="openapi", auth_strategy="bearer",
        elements=[
            Element(kind="endpoint", name="getThing", location="/things/{id}", method="GET",
                    description="read a thing", priority="high"),
            Element(kind="endpoint", name="listThings", location="/things", method="GET",
                    description="list things", priority="medium"),
        ],
        suggested_journeys=[
            Journey(name="Read a thing", description="d", priority="high", elements=["getThing"])
        ],
    )
    Workspace(root).save(inv, origin="petstore.json")


def _app(tmp_path: Path) -> AitomationApp:
    return AitomationApp(llm=_FakeLLM(), usage_log=tmp_path / "u.jsonl", workspace_root=tmp_path)


async def test_workbench_boots_empty(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#systems", DataTable).row_count == 0
        assert app.current is None


async def test_workbench_lists_and_selects_system(tmp_path):
    _seed(tmp_path)
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#systems", DataTable).row_count == 1
        assert app.current is not None and app.current.name == "Demo API"
        assert app.current_inv.elements[0].name == "getThing"
        # surface tab is populated from the inventory
        assert app.query_one("#surface", DataTable).row_count == 2
        assert app.query_one("#journeys", DataTable).row_count == 1


async def test_workbench_scaffold_action(tmp_path):
    _seed(tmp_path)
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_scaffold()
        await pilot.pause()
        run = app.workspace.latest_run("demo-api")  # timestamped run dir
        assert run is not None and run.parent.name == "e2e"
        assert (run / "conftest.py").exists()
        assert (run / "tests" / "test_smoke.py").exists()
        # flag + latest_run persisted -> stage dots advance
        rec = app.workspace.list_systems()[0]
        assert rec.scaffolded is True and rec.latest_run == str(run)


async def test_workbench_wizard_opens_and_dismisses(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")  # onboarding
        await pilot.pause()
        assert isinstance(app.screen, WizardScreen)

        app.screen.query_one("#origin", Input).value = "/tmp/does-not-exist.json"
        app.screen._submit()  # dismiss -> discovery attempted (fails fast, must not crash)
        await pilot.pause()
        await pilot.pause()

        assert not isinstance(app.screen, WizardScreen)  # back to the workbench
        assert app.workspace.list_systems() == []  # nothing saved on failure


async def test_help_overlay_opens_and_closes(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("escape")  # any key closes
        await pilot.pause()
        assert not isinstance(app.screen, HelpScreen)


async def test_delete_requires_confirmation(tmp_path):
    _seed(tmp_path)
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)

        app.screen.dismiss(False)  # cancel -> nothing deleted
        await pilot.pause()
        assert len(app.workspace.list_systems()) == 1

        await pilot.press("d")
        await pilot.pause()
        app.screen.dismiss(True)  # confirm -> deleted
        await pilot.pause()
        assert app.workspace.list_systems() == []


async def test_run_and_open_guard_when_not_scaffolded(tmp_path):
    _seed(tmp_path)
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        # system is discovered but not scaffolded -> these must no-op gracefully, not crash
        await pilot.press("t")  # run tests
        await pilot.press("o")  # open in editor
        await pilot.pause()
        assert app.current is not None  # still alive, nothing ran


async def test_run_tests_action_present(tmp_path):
    # the action exists and is a no-op without a scaffold (worker not started)
    _seed(tmp_path)
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_run_tests()  # not scaffolded -> warning, no subprocess
        await pilot.pause()
        assert app.workspace.latest_run("demo-api") is None


async def test_fix_action_gated_until_a_run_fails(tmp_path):
    _seed(tmp_path)
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        # [f] is hidden until a run has actually failed; other actions stay enabled
        assert app.check_action("fix_failing", ()) is False
        assert app.check_action("run_tests", ()) is True
        # no failing run yet -> graceful no-op (warns), no crash, no state change
        app.action_fix_failing()
        await pilot.pause()
        assert app._last_run_failed is False and app.current is not None
        # once a run fails, the affordance becomes available
        app._last_run_failed = True
        assert app.check_action("fix_failing", ()) is True


async def test_fix_runs_heal_and_clears_flag(tmp_path):
    from unittest.mock import patch
    from aitomation.write import HealReport, HealResult

    _seed(tmp_path)
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_scaffold()  # gives the system a runnable run dir
        await pilot.pause()
        app._last_run_failed = True  # pretend the last pytest run had failures

        run = Path(app.current.latest_run)
        healed = HealReport(fixed=[HealResult("Read a thing", run / "tests" / "test_x.py", fixed=True)])

        async def fake_heal(inv, provider, *, into, **kw):
            assert Path(into) == run  # heals the current run dir
            return healed

        with patch("aitomation.tui.app.heal_failing_tests", fake_heal):
            app.action_fix_failing()
            await pilot.pause()
            await pilot.pause()

        # everything fixed -> flag clears and [f] hides again
        assert app._last_run_failed is False
        assert app.check_action("fix_failing", ()) is False


def test_parse_pytest_outcomes_failure_outranks_pass():
    from aitomation.tui.app import _parse_pytest_outcomes

    summary = [
        "==== short test summary info ====",
        "PASSED tests/test_a.py::test_one",
        "PASSED tests/test_b.py::test_two",
        "FAILED tests/test_b.py::test_three - AssertionError: nope",
        "SKIPPED [1] tests/test_c.py:4: destructive",
        "ERROR tests/test_d.py - collection error",
    ]
    out = _parse_pytest_outcomes(summary)
    assert out["test_a.py"] == "passed"
    assert out["test_b.py"] == "failed"  # a failure outranks the pass in the same file
    assert out["test_d.py"] == "failed"  # error normalised to failed
    # SKIPPED lines in the summary carry a "[n]" count, not a nodeid -> not a per-file outcome
    assert "test_c.py" not in out


async def test_tests_tab_reflects_run_outcome(tmp_path):
    _seed(tmp_path)
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_scaffold()
        await pilot.pause()
        run = Path(app.current.latest_run)
        # A draft carrying a STALE "RUNTIME FAILURE" marker would read as failing on file
        # content alone...
        f = run / "tests" / "test_thing.py"
        f.write_text(
            "# RUNTIME FAILURE (still failing after self-heal): old trace\n"
            "def test_thing(api_request_context):\n    assert True\n"
        )
        app._render_tests()
        await pilot.pause()
        statuses = {n: s for n, s, _ in app._test_files}
        assert statuses["test_thing.py"] == "failing · see notes"

        # ...but once the latest run passes it, the column reflects that, not the stale marker.
        app._test_outcomes = {"test_thing.py": "passed"}
        app._render_tests()
        await pilot.pause()
        statuses = {n: s for n, s, _ in app._test_files}
        assert statuses["test_thing.py"] == "passed"

        # re-selecting rehydrates from disk: nothing was persisted here, so outcomes are empty
        app._select_system(0)
        assert app._test_outcomes == {}


async def test_run_outcomes_survive_restart(tmp_path):
    # The bug: after a run, the Tests-tab status reset to static markers on restart because
    # outcomes lived only in memory. They are now persisted per-run and rehydrated on select.
    _seed(tmp_path)
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_scaffold()
        await pilot.pause()
        run = Path(app.current.latest_run)
        (run / "tests" / "test_thing.py").write_text(
            "def test_thing(api_request_context):\n    assert True\n"
        )
        # simulate a completed run that recorded a pass, then persisted it
        app._test_outcomes = {"test_thing.py": "passed"}
        app._save_outcomes(run)
        assert (run / ".aito-status.json").is_file()

    # a fresh app instance (== a restart) over the same workspace must show 'passed', not 'ok'
    app2 = _app(tmp_path)
    async with app2.run_test() as pilot:
        await pilot.pause()
        assert app2._test_outcomes.get("test_thing.py") == "passed"
        statuses = {n: s for n, s, _ in app2._test_files}
        assert statuses["test_thing.py"] == "passed"


async def test_run_outcomes_rehydrate_from_pytest_output(tmp_path):
    # Fallback path: runs recorded before the status file existed still light up by parsing
    # the persisted pytest-output.txt.
    _seed(tmp_path)
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_scaffold()
        await pilot.pause()
        run = Path(app.current.latest_run)
        (run / "tests" / "test_thing.py").write_text(
            "def test_thing(api_request_context):\n    assert True\n"
        )
        (run / "pytest-output.txt").write_text(
            "==== short test summary info ====\nFAILED tests/test_thing.py::test_thing - boom\n"
        )
        # no .aito-status.json on purpose → must fall back to parsing pytest-output.txt
        app._select_system(0)
        assert app._test_outcomes.get("test_thing.py") == "failed"
        statuses = {n: s for n, s, _ in app._test_files}
        assert statuses["test_thing.py"] == "failed"


async def test_enable_action_gated_to_skipped_selection(tmp_path):
    from aitomation.write.generator import _SKIP_BLOCK

    _seed(tmp_path)
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_scaffold()  # gives a run dir with tests/
        await pilot.pause()
        run = Path(app.current.latest_run)
        skipped = run / "tests" / "test_create_thing.py"
        skipped.write_text(
            "# Source: Demo API (openapi) | confidence: high | DESTRUCTIVE: skipped by default\n\n\n"
            + _SKIP_BLOCK
            + "def test_create_thing(api_request_context):\n    assert True\n"
        )
        app._render_tests()  # pick up the seeded skip-guarded draft
        await pilot.pause()

        table = app.query_one("#tests", DataTable)
        # highlight the non-skipped smoke test -> [e] hidden
        ok_idx = next(i for i, (_n, s, _p) in enumerate(app._test_files) if s == "ok")
        table.move_cursor(row=ok_idx)
        assert app.check_action("enable_test", ()) is False

        # highlight the skipped draft -> [e] offered
        skip_idx = next(i for i, (_n, s, _p) in enumerate(app._test_files) if "skip" in s)
        table.move_cursor(row=skip_idx)
        assert app.check_action("enable_test", ()) is True


async def test_enable_action_lifts_skip_after_confirm(tmp_path):
    from aitomation.write.generator import _SKIP_BLOCK

    _seed(tmp_path)
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_scaffold()
        await pilot.pause()
        run = Path(app.current.latest_run)
        skipped = run / "tests" / "test_create_thing.py"
        skipped.write_text(
            "# Source: Demo API (openapi) | confidence: high | DESTRUCTIVE: skipped by default\n\n\n"
            + _SKIP_BLOCK
            + "def test_create_thing(api_request_context):\n    assert True\n"
        )
        app._render_tests()
        await pilot.pause()

        idx = next(i for i, (_n, s, _p) in enumerate(app._test_files) if "skip" in s)
        app.query_one("#tests", DataTable).move_cursor(row=idx)

        # enabling is confirmation-gated (it makes a mutating test runnable)
        app.action_enable_test()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        app.screen.dismiss(True)  # confirm
        await pilot.pause()
        await pilot.pause()

        # guard lifted on disk, header note updated, and the panel status flips to ok
        src = skipped.read_text()
        assert "mark.skip" not in src
        assert "DESTRUCTIVE: enabled (skip lifted" in src
        statuses = {n: s for n, s, _p in app._test_files}
        assert statuses["test_create_thing.py"] == "ok"


async def test_enable_action_cancel_keeps_skip(tmp_path):
    from aitomation.write.generator import _SKIP_BLOCK

    _seed(tmp_path)
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_scaffold()
        await pilot.pause()
        run = Path(app.current.latest_run)
        skipped = run / "tests" / "test_create_thing.py"
        skipped.write_text(_SKIP_BLOCK + "def test_create_thing(api_request_context):\n    assert True\n")
        app._render_tests()
        await pilot.pause()
        idx = next(i for i, (_n, s, _p) in enumerate(app._test_files) if "skip" in s)
        app.query_one("#tests", DataTable).move_cursor(row=idx)

        app.action_enable_test()
        await pilot.pause()
        app.screen.dismiss(False)  # cancel -> the guard stays
        await pilot.pause()
        assert "mark.skip" in skipped.read_text()


async def test_model_picker_opens_and_closes(tmp_path):
    _seed(tmp_path)
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("m")  # or click the title bar
        await pilot.pause()
        assert isinstance(app.screen, ModelScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, ModelScreen)


async def test_clicking_title_bar_opens_model_picker(tmp_path):
    from textual.widgets._header import HeaderTitle

    _seed(tmp_path)
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.click(HeaderTitle)  # click the "aitomation — backend:model" title
        await pilot.pause()
        assert isinstance(app.screen, ModelScreen)


async def test_model_choice_applies(tmp_path, monkeypatch):
    for var in _LLM_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._apply_model_choice("anthropic", "claude-opus-4-8")
        await pilot.pause()
        assert app.sub_title == "anthropic:claude-opus-4-8"
        assert app._config.backend == "anthropic" and app._config.model == "claude-opus-4-8"
        assert app._config.output_mode == "tool"  # anthropic does reliable tool calling


async def test_model_choice_rejects_backend_without_key(tmp_path, monkeypatch):
    for var in _LLM_ENV:
        monkeypatch.delenv(var, raising=False)
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.sub_title = "before"
        app._apply_model_choice("dashscope", "qwen-plus")  # no DASHSCOPE_API_KEY -> rejected
        await pilot.pause()
        assert app.sub_title == "before"  # unchanged
        assert app._config is None  # nothing applied


def test_find_app_bundle_prefers_exact_then_prefix(tmp_path):
    from aitomation.tui.app import _find_app_bundle

    (tmp_path / "PyCharm.app").mkdir()
    (tmp_path / "Antigravity IDE.app").mkdir()
    roots = (tmp_path,)
    # exact match wins
    assert _find_app_bundle(("PyCharm",), roots).name == "PyCharm.app"
    # falls back to a prefix match (and honours candidate order)
    assert _find_app_bundle(("Antigravity IDE", "Antigravity"), roots).name == "Antigravity IDE.app"
    assert _find_app_bundle(("Nope",), roots) is None


def test_resolve_editor_macos_opens_app_bundle(tmp_path, monkeypatch):
    import aitomation.tui.app as appmod

    monkeypatch.setattr(appmod, "_MACOS", True)
    monkeypatch.setattr(appmod, "_APP_ROOTS", (tmp_path,))
    (tmp_path / "Cursor.app").mkdir()
    launch = appmod._resolve_editor(("cursor",), ("Cursor",))
    assert launch[:2] == ["open", "-a"] and launch[2].endswith("Cursor.app")
    # not installed -> None even though a CLI might exist elsewhere
    assert appmod._resolve_editor(("pycharm", "charm"), ("PyCharm",)) is None


def test_resolve_editor_non_macos_uses_cli(monkeypatch):
    import aitomation.tui.app as appmod

    monkeypatch.setattr(appmod, "_MACOS", False)
    monkeypatch.setattr(appmod.shutil, "which", lambda c: "/usr/bin/code" if c == "code" else None)
    assert appmod._resolve_editor(("code",), ("Visual Studio Code",)) == ["code"]
    assert appmod._resolve_editor(("pycharm", "charm"), ("PyCharm",)) is None


async def test_open_editor_shows_picker(tmp_path, monkeypatch):
    import aitomation.tui.app as appmod
    from aitomation.tui.app import EditorScreen
    from textual.widgets import Button

    # only VS Code installed; the rest must appear disabled
    def fake_resolve(cli, apps):
        return ["open", "-a", "/Applications/Visual Studio Code.app"] if "code" in cli else None

    monkeypatch.setattr(appmod, "_resolve_editor", fake_resolve)
    _seed(tmp_path)
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_scaffold()  # need a run dir to open
        await pilot.pause()
        await pilot.press("o")
        await pilot.pause()
        assert isinstance(app.screen, EditorScreen)
        labels = {b.label.plain: b.disabled for b in app.screen.query(Button) if b.id != "cancel"}
        assert labels.get("VS Code") is False
        assert any("PyCharm" in name and disabled for name, disabled in labels.items())


async def test_open_editor_launches_chosen(tmp_path, monkeypatch):
    import aitomation.tui.app as appmod

    calls: list = []
    monkeypatch.setattr(appmod.subprocess, "Popen", lambda args, *a, **k: calls.append(args))
    _seed(tmp_path)
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_scaffold()
        await pilot.pause()
        run = app.current.latest_run
        app._on_editor_chosen(["open", "-a", "/Applications/Cursor.app"])  # picked Cursor
        await pilot.pause()
        assert calls == [["open", "-a", "/Applications/Cursor.app", run]]


async def test_open_editor_no_editor_falls_back(tmp_path, monkeypatch):
    import aitomation.tui.app as appmod
    from aitomation.tui.app import EditorScreen

    monkeypatch.setattr(appmod, "_resolve_editor", lambda cli, apps: None)  # nothing installed
    _seed(tmp_path)
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_scaffold()
        await pilot.pause()
        app.action_open_editor()  # no editors -> graceful notify, no picker
        await pilot.pause()
        assert not isinstance(app.screen, EditorScreen)


async def test_wizard_requires_origin(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        wizard = app.screen
        assert isinstance(wizard, WizardScreen)
        wizard._submit()  # empty origin -> stays open
        await pilot.pause()
        assert isinstance(app.screen, WizardScreen)


# -- wizard surfaces the backend discovery sources (Tier 1) -----------------------------


async def test_wizard_offers_backend_sources(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        rs = app.screen.query_one("#source", RadioSet)
        labels = " ".join(str(b.label) for b in rs.query(RadioButton))
        assert len([*rs.query(RadioButton)]) == 5
        assert "AsyncAPI" in labels and "Schema registry" in labels and "Database" in labels


def test_wizard_source_keys_in_order():
    # _submit() maps the selected radio index to this key; run_discover dispatches on it.
    from aitomation.tui.app import _WIZARD_SOURCES

    assert [s[0] for s in _WIZARD_SOURCES] == ["openapi", "crawl", "asyncapi", "registry", "db"]


async def _run_one_discover(app, pilot, source: str, origin: str, fn_name: str) -> dict:
    """Fire run_discover for one source with the named discover fn patched out, and report
    the origin it was called with. The patched fn returns a minimal inventory so the worker
    completes (save/refresh) without a real model or network."""
    from unittest.mock import patch

    called: dict = {}

    async def fake(origin_, provider):
        called["origin"] = origin_
        return CoverageInventory(
            system_name=f"X-{origin_}", base_url=origin_, source="openapi",
            elements=[Element(kind="endpoint", name="e", location="/e", method="GET",
                              description="x", priority="low")],
        )

    with patch(f"aitomation.tui.app.{fn_name}", fake):
        app.run_discover(source, origin, None)
        await pilot.pause()
        await pilot.pause()
    return called


async def test_tui_dispatches_new_discovery_sources(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert (await _run_one_discover(app, pilot, "asyncapi", "a.yaml", "discover_asyncapi"))["origin"] == "a.yaml"
        assert (await _run_one_discover(app, pilot, "registry", "http://r", "discover_registry"))["origin"] == "http://r"
        assert (await _run_one_discover(app, pilot, "db", "x.sql", "discover_db"))["origin"] == "x.sql"
        # re-discover passes the inventory's DiscoverySource — both forms must route correctly
        assert (await _run_one_discover(app, pilot, "schema_registry", "http://r2", "discover_registry"))["origin"] == "http://r2"
        assert (await _run_one_discover(app, pilot, "db_schema", "y.sql", "discover_db"))["origin"] == "y.sql"


async def test_header_title_shimmers(tmp_path):
    # Cosmetic: the header title animates a light-sweep. Verify it still renders the real
    # title text and that the sweep phase advances on tick.
    from aitomation.tui.app import ShimmerTitle

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        title = app.query_one(ShimmerTitle)
        assert "aitomation" in title.render().plain  # per-char styling, same text
        before = title._phase
        title._advance()
        assert title._phase != before  # the comet moved

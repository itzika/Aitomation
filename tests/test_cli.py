"""Guard tests for the CLI: it must import cleanly and register all commands.

The unit suite otherwise imports submodules directly, so an import-time error in cli.py
(e.g. a default that references a not-yet-defined name) would slip through. This catches it."""

from __future__ import annotations

import re

from typer.testing import CliRunner

from aitomation.cli import app
from aitomation.models import CoverageInventory, Journey
from aitomation.models import TestableElement as Element
from aitomation.workspace import Workspace
from aitomation.write.generator import _SKIP_BLOCK

runner = CliRunner()

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(result) -> str:
    """Help output with ANSI styling stripped. Typer's option highlighter splits flag tokens
    like `--force` with colour spans when colour is forced (CI sets FORCE_COLOR, which Rich
    honours over NO_COLOR), so a raw substring check on the styled output is brittle."""
    return _ANSI.sub("", result.output)


def _inventory_file(tmp_path, name="Rick & Morty API"):
    inv = CoverageInventory(
        system_name=name,
        base_url="https://rickandmortyapi.com/api",
        source="openapi",
        elements=[
            Element(
                kind="endpoint",
                name="list_chars",
                location="/character",
                method="GET",
                description="List characters",
                priority="high",
            ),
        ],
        suggested_journeys=[Journey(name="Browse", description="d", priority="high")],
    )
    p = tmp_path / "inv.json"
    p.write_text(inv.model_dump_json(indent=2), encoding="utf-8")
    return p


def test_cli_help_lists_all_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    out = _plain(result)
    for command in ("discover", "go", "scaffold", "write", "schema", "usage", "tui", "version"):
        assert command in out
    # workflow order, not definition order: the help reads as the pipeline
    assert out.index("tui") < out.index("go ") < out.index("scaffold") < out.index("version")


def test_schema_command_prints_versioned_json_schema():
    import json

    result = runner.invoke(app, ["schema"])
    assert result.exit_code == 0
    schema = json.loads(result.output)
    assert schema["properties"]["schema_version"]["default"] == 1


def test_sniff_kind_detects_sources(tmp_path):
    from aitomation.cli import _sniff_kind

    assert _sniff_kind("postgresql://u@h/db") == "db"
    assert _sniff_kind("./migrations/schema.sql") == "db"
    spec = tmp_path / "spec.json"
    spec.write_text('{"openapi": "3.0.3"}', encoding="utf-8")
    assert _sniff_kind(str(spec)) == "openapi"
    aspec = tmp_path / "events.yaml"
    aspec.write_text("asyncapi: 3.0.0\n", encoding="utf-8")
    assert _sniff_kind(str(aspec)) == "asyncapi"


class _GoFakeProvider:
    """Stands in for the LLM across the whole `go` pipeline: discovery judgement + drafts."""

    async def generate(self, prompt, *, system=None, label=""):  # pragma: no cover
        return ""

    async def generate_structured(self, prompt, schema, *, system=None, label=""):
        if schema.__name__ == "InventoryJudgment":
            return schema(
                system_summary="Petstore.",
                auth_strategy="bearer",
                high_priority=[],
                low_priority=[],
                suggested_journeys=[
                    {
                        "name": "Browse pets",
                        "description": "list pets",
                        "priority": "high",
                        "steps": [],
                        "elements": ["listPets"],
                    }
                ],
            )
        if schema.__name__ == "TestDraft":
            return schema(
                code="def test_ok(api_request_context):\n    assert True\n",
                confidence="high",
                review_notes="",
            )
        raise AssertionError(f"unexpected schema {schema.__name__}")


def test_go_runs_full_pipeline(tmp_path, monkeypatch):
    """One command: discover (stubbed model) → scaffold → drafts, all in the shared
    workspace, with pipeline flags set so the TUI lists the system as fully processed."""
    import pathlib

    spec = pathlib.Path(__file__).parent.parent / "examples" / "petstore-mini.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("aitomation.cli._resolve_provider", lambda *a, **k: _GoFakeProvider())

    result = runner.invoke(app, ["go", str(spec)])
    assert result.exit_code == 0, result.output

    runs = list((tmp_path / "projects").glob("*/e2e/run-*"))
    assert len(runs) == 1
    assert (runs[0] / "conftest.py").is_file()
    assert (runs[0] / "api" / "client.py").is_file()  # package-layout scaffold
    drafted = list((runs[0] / "tests").rglob("test_*.py"))
    assert any(p.parent.name == "api" for p in drafted)  # draft routed by surface

    systems = Workspace("projects").list_systems()
    assert len(systems) == 1
    assert systems[0].scaffolded is True and systems[0].drafted is True


def test_cli_version():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "aitomation" in result.output


def test_discover_subcommands_present():
    result = runner.invoke(app, ["discover", "--help"])
    assert result.exit_code == 0
    out = _plain(result)
    for sub in ("openapi", "crawl", "asyncapi", "registry", "db"):
        assert sub in out


def test_discover_db_help_documents_both_modes():
    result = runner.invoke(app, ["discover", "db", "--help"])
    assert result.exit_code == 0
    out = _plain(result)
    assert "DDL" in out or ".sql" in out


def test_write_help_has_force_flag():
    result = runner.invoke(app, ["write", "--help"])
    assert result.exit_code == 0
    assert "--force" in _plain(result)


def test_cli_try_load_inventory_reads_baseline(tmp_path):
    # the auto-baseline helper round-trips a written inventory and tolerates junk
    from aitomation.cli import _try_load_inventory
    from aitomation.models import CoverageInventory
    from aitomation.models import TestableElement as Element

    p = tmp_path / "inventory.json"
    assert _try_load_inventory(p) is None  # nothing there yet
    inv = CoverageInventory(
        system_name="Demo",
        base_url="https://x",
        source="openapi",
        elements=[
            Element(
                kind="endpoint",
                name="g",
                location="/x",
                method="GET",
                description="d",
                priority="high",
            )
        ],
    )
    p.write_text(inv.model_dump_json(), encoding="utf-8")
    assert _try_load_inventory(p).system_name == "Demo"
    (tmp_path / "junk.json").write_text("not an inventory", encoding="utf-8")
    assert _try_load_inventory(tmp_path / "junk.json") is None


def test_scaffold_default_routes_through_shared_workspace(tmp_path, monkeypatch):
    # No --out → the CLI must use the same projects/<slug>/e2e/run-*/ layout the TUI uses,
    # and register the system in the shared index so the TUI library lists it.
    monkeypatch.chdir(tmp_path)
    inv_path = _inventory_file(tmp_path)
    result = runner.invoke(app, ["scaffold", str(inv_path)])
    assert result.exit_code == 0, result.output

    runs = list((tmp_path / "projects").glob("*/e2e/run-*"))
    assert len(runs) == 1, "scaffold did not use the workspace run-dir layout"
    assert (runs[0] / "conftest.py").is_file()

    systems = Workspace("projects").list_systems()
    assert len(systems) == 1 and systems[0].scaffolded is True
    assert systems[0].latest_run and (tmp_path / systems[0].latest_run) == runs[0]


def test_enable_resolves_workspace_run_dirs(tmp_path, monkeypatch):
    # `enable -i projects` must reach drafts under projects/<slug>/e2e/run-*/tests via the
    # shared Workspace — the layout the TUI writes — not just flat projects/<slug>/tests.
    monkeypatch.chdir(tmp_path)
    inv_path = _inventory_file(tmp_path)
    assert runner.invoke(app, ["scaffold", str(inv_path)]).exit_code == 0
    run = next((tmp_path / "projects").glob("*/e2e/run-*"))

    guarded = _SKIP_BLOCK + "def test_delete():\n    assert True\n"
    (run / "tests").mkdir(exist_ok=True)
    (run / "tests" / "test_delete.py").write_text("# Flow: x\n\n" + guarded, encoding="utf-8")

    result = runner.invoke(app, ["enable", "--all", "-i", "projects"])
    assert result.exit_code == 0, result.output
    assert _SKIP_BLOCK not in (run / "tests" / "test_delete.py").read_text(encoding="utf-8")

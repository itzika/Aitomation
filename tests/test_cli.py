"""Guard tests for the CLI: it must import cleanly and register all commands.

The unit suite otherwise imports submodules directly, so an import-time error in cli.py
(e.g. a default that references a not-yet-defined name) would slip through. This catches it."""

from __future__ import annotations

from typer.testing import CliRunner

from aitomation.cli import app

runner = CliRunner()


def test_cli_help_lists_all_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("discover", "scaffold", "write", "usage", "tui", "version"):
        assert command in result.output


def test_cli_version():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "aitomation" in result.output


def test_discover_subcommands_present():
    result = runner.invoke(app, ["discover", "--help"])
    assert result.exit_code == 0
    for sub in ("openapi", "crawl", "asyncapi", "registry", "db"):
        assert sub in result.output


def test_discover_db_help_documents_both_modes():
    result = runner.invoke(app, ["discover", "db", "--help"])
    assert result.exit_code == 0
    assert "DDL" in result.output or ".sql" in result.output


def test_write_help_has_force_flag():
    result = runner.invoke(app, ["write", "--help"])
    assert result.exit_code == 0
    assert "--force" in result.output


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

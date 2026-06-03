"""Tests for workspace persistence — the browsable systems library."""

from __future__ import annotations

from aitomation.models import CoverageInventory, Journey
from aitomation.models import TestableElement as Element  # aliased: avoid pytest "Test*" collection
from aitomation.tui.workspace import Workspace, slugify


def _inv(name: str = "Demo API") -> CoverageInventory:
    return CoverageInventory(
        system_name=name,
        base_url="https://x",
        source="openapi",
        elements=[
            Element(
                kind="endpoint",
                name="getX",
                location="/x",
                method="GET",
                description="d",
                priority="high",
            )
        ],
        suggested_journeys=[Journey(name="J", description="d", priority="high")],
    )


def test_slugify():
    assert slugify("Rick & Morty API") == "rick-morty-api"
    assert slugify("Swagger Petstore - OpenAPI 3.0") == "swagger-petstore-openapi-3-0"
    assert slugify("") == "system"


def test_save_list_load_roundtrip(tmp_path):
    ws = Workspace(tmp_path)
    rec = ws.save(_inv(), origin="spec.json")
    assert rec.slug == "demo-api" and rec.n_elements == 1 and rec.n_journeys == 1

    listed = ws.list_systems()
    assert len(listed) == 1 and listed[0].name == "Demo API" and listed[0].origin == "spec.json"

    inv = ws.load_inventory("demo-api")
    assert inv.system_name == "Demo API" and inv.elements[0].name == "getX"


def test_stage_dots_track_flags(tmp_path):
    ws = Workspace(tmp_path)
    rec = ws.save(_inv(), origin="x")
    assert rec.stage_dots == "●○○"
    assert ws.set_flags("demo-api", scaffolded=True).stage_dots == "●●○"
    assert ws.set_flags("demo-api", drafted=True).stage_dots == "●●●"


def test_delete_and_empty(tmp_path):
    ws = Workspace(tmp_path)
    assert ws.list_systems() == []
    ws.save(_inv(), origin="x")
    ws.delete("demo-api")
    assert ws.list_systems() == []


def test_try_load_inventory_baseline_and_missing(tmp_path):
    ws = Workspace(tmp_path)
    # no system saved yet → no baseline to diff against
    assert ws.try_load_inventory("demo-api") is None
    ws.save(_inv(), origin="x")
    loaded = ws.try_load_inventory("demo-api")
    assert loaded is not None and loaded.system_name == "Demo API"
    assert loaded.elements[0].name == "getX"


def test_resave_preserves_pipeline_state(tmp_path):
    ws = Workspace(tmp_path)
    ws.save(_inv(), origin="x")
    ws.set_flags("demo-api", scaffolded=True, drafted=True, latest_run="e2e/run-1")
    ws.save(_inv(), origin="x2")  # re-discover same system (incremental, non-destructive)
    listed = ws.list_systems()
    assert len(listed) == 1 and listed[0].origin == "x2"
    # re-discovery keeps prior scaffold/drafts and the run that holds them — you don't
    # have to scaffold + write everything again when nothing (or little) changed.
    assert listed[0].scaffolded is True and listed[0].drafted is True
    assert listed[0].latest_run == "e2e/run-1"


def test_save_explicit_flags_override(tmp_path):
    ws = Workspace(tmp_path)
    ws.save(_inv(), origin="x")
    ws.set_flags("demo-api", scaffolded=True, drafted=True)
    # an explicit reset is still honoured (e.g. a deliberate fresh start)
    rec = ws.save(_inv(), origin="x", scaffolded=False, drafted=False)
    assert rec.scaffolded is False and rec.drafted is False

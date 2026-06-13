"""Regenerate the README screenshots of the Workbench TUI.

Drives the real app headlessly via Textual's pilot against the checked-in `projects/`
workspace, exports each screen to SVG, then rasterises to PNG with `rsvg-convert`
(install: `brew install librsvg`). Run from the repo root:

    uv run python scripts/screenshots.py

PNGs (referenced by README.md) land in docs/img/. Re-run after a UI change to refresh
them.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

from dotenv import load_dotenv
from textual.widgets import DataTable, TabbedContent

from aitomation.tui.app import AitomationApp

OUT = Path("docs/img")
SIZE = (120, 36)
# Row 0 is the richest system (scaffolded + drafted + run), so the tabs have real content.


def _to_png(svg: Path) -> None:
    png = svg.with_suffix(".png")
    subprocess.run(["rsvg-convert", str(svg), "-o", str(png)], check=True)
    svg.unlink()  # keep only the PNGs the README points at


async def _shot(app: AitomationApp, pilot, name: str) -> None:
    await pilot.pause()
    await asyncio.sleep(0.35)
    app.save_screenshot(str(OUT / f"{name}.svg"))


def _select_row(app: AitomationApp, table_id: str) -> None:
    table = app.query_one(f"#{table_id}", DataTable)
    if table.row_count:
        table.move_cursor(row=0)  # fires RowHighlighted → populates the detail pane


def _richest(app: AitomationApp) -> int:
    """Index of the most demo-worthy system: fully pipelined (so Tests/Usage are populated),
    then most elements — stable regardless of what else is in the local projects/ workspace."""
    recs = app._records
    if not recs:
        return 0
    return max(
        range(len(recs)),
        key=lambda i: (recs[i].drafted, recs[i].scaffolded, recs[i].n_elements),
    )


async def _capture_credentials() -> None:
    """The credentials modal, on a throwaway session-auth system in a temp workspace (the real
    projects/ systems may have no auth). The secret store is forced to an isolated encrypted
    file via env in main(), so this never touches the real keychain."""
    import tempfile

    from aitomation.credentials import set_credential
    from aitomation.models import CoverageInventory, InputField, Journey
    from aitomation.models import TestableElement as El
    from aitomation.workspace import Workspace

    ws = Path(tempfile.mkdtemp())
    inv = CoverageInventory(
        system_name="Acme Shop",
        base_url="https://shop.acme.test/",
        source="crawl",
        auth_strategy="session",
        elements=[
            El(
                kind="form",
                name="login",
                location="/login",
                description="Form on /login (login)",
                preconditions=["requires authenticated session"],
                priority="high",
                inputs=[
                    InputField(
                        name="username",
                        type="text",
                        where="form",
                        locator='get_by_label("Username")',
                    ),
                    InputField(
                        name="password",
                        type="password",
                        where="form",
                        locator='get_by_label("Password")',
                    ),
                ],
            ),
            El(
                kind="page",
                name="catalog",
                location="/catalog",
                description="catalog",
                priority="high",
            ),
        ],
        suggested_journeys=[
            Journey(
                name="Sign in and check out", description="d", priority="high", elements=["login"]
            )
        ],
    )
    rec = Workspace(ws).save(inv, origin="https://shop.acme.test")
    # Pre-store a couple so the modal shows a realistic mix (BASE_URL + username set, password not).
    set_credential(rec.slug, "dev", "BASE_URL", "https://dev.shop.acme.test")
    set_credential(rec.slug, "dev", "AUTH_USER", "qa@acme.test")

    app = AitomationApp(workspace_root=ws, usage_log="usage.jsonl")
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.action_credentials()
        await pilot.pause()
        await asyncio.sleep(0.3)
        app.save_screenshot(str(OUT / "credentials.svg"))
        await pilot.pause()


async def main() -> None:
    load_dotenv()  # so the banner shows the real configured provider:model
    # Isolate the secret store: a temp encrypted file, never the real keychain or ~/.config.
    import tempfile

    os.environ["AITOMATION_SECRETS_BACKEND"] = "file"
    os.environ["AITOMATION_SECRETS_FILE"] = str(Path(tempfile.mkdtemp()) / "secrets.enc")
    os.environ["AITOMATION_VAULT_PASSPHRASE"] = "screenshot-only"
    OUT.mkdir(parents=True, exist_ok=True)
    app = AitomationApp(workspace_root="projects", usage_log="usage.jsonl")
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        tabs = app.query_one("#tabs", TabbedContent)

        # Anchor the walkthrough on the richest system so every tab has real content.
        app.query_one("#systems", DataTable).move_cursor(row=_richest(app))
        await pilot.pause()
        await _shot(app, pilot, "overview")

        tabs.active = "tab-surface"
        _select_row(app, "surface")
        await _shot(app, pilot, "coverage")

        tabs.active = "tab-journeys"
        _select_row(app, "journeys")
        await _shot(app, pilot, "flows")

        tabs.active = "tab-tests"

        # Prefer a passing draft with a clean header (no stale RUNTIME-FAILURE provenance note),
        # so the detail pane shows a tidy source preview. Status colours show in the column
        # regardless.
        def _clean_pass(i: int) -> bool:
            _name, status, path = app._test_files[i]
            return status == "passed" and "RUNTIME FAILURE" not in path.read_text("utf-8")

        rows = range(len(app._test_files))
        row = next((i for i in rows if _clean_pass(i)), 0)
        app.query_one("#tests", DataTable).move_cursor(row=row)
        await _shot(app, pilot, "tests")

        tabs.active = "tab-usage"
        await asyncio.sleep(0.6)  # let the usage meters finish their ease-in
        await _shot(app, pilot, "usage")

        tabs.active = "tab-overview"
        app.action_new_system()  # the onboarding wizard modal
        await _shot(app, pilot, "wizard")
        await pilot.press("escape")

        app.action_choose_model()  # the provider / model picker modal
        await asyncio.sleep(2.0)  # give the live /models listing a chance to fill
        await _shot(app, pilot, "model")
        await pilot.press("escape")

    await _capture_credentials()  # the credentials modal (separate temp workspace)

    for svg in sorted(OUT.glob("*.svg")):
        _to_png(svg)
    print(f"wrote {len(list(OUT.glob('*.png')))} screenshots to {OUT}/")


if __name__ == "__main__":
    if not shutil.which("rsvg-convert"):
        raise SystemExit("need rsvg-convert (brew install librsvg)")
    asyncio.run(main())

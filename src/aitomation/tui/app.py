"""The Workbench TUI: a browsable Systems library (master) with a tabbed System view
(detail), a live log, an onboarding wizard, and a command palette.

Cyberpunk-leaning visual language: a custom neon-on-near-black theme, restrained chrome,
status conveyed by text/badges. It drives the same pipeline as the CLI; generated artifacts
land in visible, timestamped per-run directories under each tested app.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import get_args

from rich.markup import escape
from rich.syntax import Syntax
from rich.table import Table as RichTable
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Hit, Hits, Provider
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Input,
    Label,
    ProgressBar,
    RadioButton,
    RadioSet,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

from ..config import Backend, ConfigError, LLMConfig
from ..diff import diff_inventories
from ..discover.asyncapi import discover_asyncapi
from ..discover.crawl import discover_crawl
from ..discover.database import discover_db
from ..discover.openapi import discover_openapi
from ..discover.registry import discover_registry
from ..providers import LLMProvider, PydanticAIProvider
from ..scaffold import scaffold_project
from ..scaffold.generator import _func_name
from ..telemetry import DEFAULT_LOG, UsageRecorder, aggregate, load_records
from ..write import draft_tests, enable_drafts, heal_failing_tests, select_journeys
from ..naming import PROJECTS_ROOT
from .workspace import SystemRecord, Workspace, slugify

CYBERPUNK = Theme(
    name="cyberpunk",
    primary="#22d3ee",     # cyan
    secondary="#f637ec",   # magenta
    accent="#39ff14",      # neon green
    foreground="#cde7f0",
    background="#080b12",
    surface="#0e1320",
    panel="#141b2d",
    success="#39ff14",
    warning="#ffd400",
    error="#ff2e63",
    dark=True,
    variables={
        "block-cursor-foreground": "#080b12",
        "block-cursor-background": "#22d3ee",
        "footer-key-foreground": "#39ff14",
    },
)

_HELP = """\
[b]aitomation — Workbench[/b]

A system moves through three stages, shown as dots in the library: \
[b]discover[/b] · [b]scaffold[/b] · [b]write[/b].

[b]Keys[/b]
  n   discover a new system (guided wizard)
  s   scaffold a runnable pytest + playwright project (new timestamped run)
  w   draft tests for new flows (keeps existing drafts; review-only)
  r   re-discover — reports what changed since last time
  t   run the scaffolded tests here (pytest decides pass/fail, never the AI)
  f   fix: self-heal the tests that just failed (one corrective retry each)
  e   enable the selected skipped (destructive) draft — review + add teardown first
  o   open the run folder — pick VS Code / PyCharm / Cursor / Antigravity
  m   change the provider/model (or click the title in the header)
  d   delete the selected system
  l   toggle the live log
  b   fold / unfold the animated header banner
  ↑/↓ move · enter open · tab switch panes
  Ctrl+P   command palette
  ?   this help · q quit

[b]Tabs[/b]
  Overview  system facts, auth, counts, pipeline stage, token cost
  Coverage  every testable element; select one for its inputs & preconditions
  Flows     suggested end-to-end paths; select for steps
  Tests     drafts + source preview; status reflects the last run (passed / failed /
            skipped / needs review)
  Usage     LLM token cost for this system, by prompt

[dim]press any key to close[/]"""


_BACKENDS: tuple[str, ...] = get_args(Backend)

# Colour the Tests-tab status so pass/fail/skip read at a glance.
_STATUS_STYLE = {
    "passed": "#39ff14",
    "ok": "#39ff14",
    "failed": "#ff2e63",
    "failing · see notes": "#ff2e63",
    "skipped": "#ffd400",
    "skipped · destructive": "#ffd400",
    "needs review": "#ffd400",
}

# Latest per-file run outcomes, persisted next to pytest-output.txt in a run dir so the
# Tests-tab status survives a TUI restart instead of resetting to static file markers.
_STATUS_FILE = ".aito-status.json"

# Outcome precedence when a file has several tests: failure/error > pass > skip.
_OUTCOME_RANK = {"skipped": 0, "xfail": 0, "xpass": 0, "passed": 1, "failed": 2, "error": 2}
_OUTCOME_LINE = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\s+(\S+?\.py)(?:::|\s|$)")


def _status_text(status: str) -> Text:
    return Text(status, style=_STATUS_STYLE.get(status, ""))


def _parse_pytest_outcomes(lines: list[str]) -> dict[str, str]:
    """Map test-file name -> worst outcome from a pytest run's `-rA` short summary.

    A file with any failed/error test is 'failed'; otherwise 'passed' if it had a pass, or
    'skipped'. 'error' is normalised to 'failed' for display."""
    out: dict[str, str] = {}
    for ln in lines:
        m = _OUTCOME_LINE.match(ln.strip())
        if not m:
            continue
        outcome = m.group(1).lower()
        name = Path(m.group(2)).name
        prev = out.get(name)
        if prev is None or _OUTCOME_RANK.get(outcome, 0) > _OUTCOME_RANK.get(prev, 0):
            out[name] = outcome
    return {n: ("failed" if o == "error" else o) for n, o in out.items()}

# Editors offered by the "open run folder" picker (o): (display, CLI candidates, macOS .app
# name prefixes). GUI editors on macOS rarely put their CLI on PATH (only Cursor tends to),
# so we detect/launch by .app bundle there; CLI is the fallback (and the Linux/Windows path).
_EDITORS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("VS Code", ("code",), ("Visual Studio Code",)),
    ("PyCharm", ("pycharm", "charm"), ("PyCharm",)),
    ("Cursor", ("cursor",), ("Cursor",)),
    # "Antigravity IDE.app" is the Electron editor; plain "Antigravity.app" is the launcher.
    ("Antigravity", ("antigravity",), ("Antigravity IDE", "Antigravity")),
)

_MACOS = sys.platform == "darwin"
_APP_ROOTS: tuple[Path, ...] = (Path("/Applications"), Path.home() / "Applications")


def _find_app_bundle(prefixes: tuple[str, ...], roots: tuple[Path, ...] | None = None) -> Path | None:
    """Locate an installed .app for one of `prefixes`, exact name first then prefix match."""
    roots = roots if roots is not None else _APP_ROOTS
    for pre in prefixes:
        for root in roots:
            exact = root / f"{pre}.app"
            if exact.exists():
                return exact
        for root in roots:
            if root.is_dir():
                matches = sorted(root.glob(f"{pre}*.app"))
                if matches:
                    return matches[0]
    return None


def _resolve_editor(cli_candidates: tuple[str, ...], app_prefixes: tuple[str, ...]) -> list[str] | None:
    """Return the argv prefix that launches this editor (the run dir is appended later), or
    None if it isn't installed. On macOS prefer `open -a <bundle>` since the CLI is usually
    absent; everywhere else (and as a fallback) use the CLI launcher if it's on PATH."""
    if _MACOS:
        bundle = _find_app_bundle(app_prefixes)
        if bundle is not None:
            return ["open", "-a", str(bundle)]
    cmd = next((c for c in cli_candidates if shutil.which(c)), None)
    return [cmd] if cmd else None


# Title shimmer: a single-hue (dim cyan -> white) comet head with a short fading tail that
# sweeps across the letters, with a beat between sweeps. Single-hue on purpose, so it reads as
# light gliding over chrome rather than adding to a colour clash.
_SHIMMER_TIERS = ("#155e6b", "#22d3ee", "#7eecff", "#ffffff")  # base, tail, mid, comet head
_SHIMMER_GAP = 8  # cells of "pause" appended to the sweep so it pulses instead of running solid

# Matrix-rain backdrop for the header band: ASCII glyphs falling in per-column streaks, each a
# bright white head trailing a cyan tail that fades to near-black. Same cyan→white family as the
# title shimmer so the band reads as one effect, not a palette pile-up.
_RAIN_GLYPHS = "01<>[]{}/\\=+*#$%&!?:;~^|0123456789ABCDEF"
_RAIN_HEAD = "#eafcff"
_RAIN_FADE = ("#7eecff", "#22d3ee", "#1a8aa0", "#125663")
_BAND_HEIGHT = 7


def _shimmer_style(offset: int) -> str:
    """Style for a title char `offset` cells behind the sweeping comet head."""
    if offset == 0:
        return f"bold {_SHIMMER_TIERS[3]}"
    if offset == 1:
        return _SHIMMER_TIERS[2]
    if offset == 2:
        return _SHIMMER_TIERS[1]
    return _SHIMMER_TIERS[0]


def _rain_style(d: int, length: int) -> str:
    """Style for a rain cell `d` cells above its column's head, in a streak of `length`."""
    if d == 0:
        return f"bold {_RAIN_HEAD}"
    f = d / max(length, 1)
    if f < 0.2:
        return _RAIN_FADE[0]
    if f < 0.45:
        return _RAIN_FADE[1]
    if f < 0.75:
        return _RAIN_FADE[2]
    return _RAIN_FADE[3]


class MatrixBanner(Static):
    """The header: a matrix-rain ASCII animation across the full width with the 'aitomation'
    title (shimmering) and the active model overlaid and centred. Clicking the title opens the
    model picker (same as `m`). Collapsible to a single line (press `b`) and PAUSED while an
    operation runs, so it's a flourish rather than a persistent, distracting backdrop."""

    DEFAULT_CSS = """
    MatrixBanner {
        dock: top;
        width: 100%;
        height: 7;
        background: $background;
        color: $foreground;
    }
    MatrixBanner.-folded { height: 1; }
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._expanded = True
        self._paused = False
        self._phase = 0  # shimmer comet position
        self._w = self._h = 0  # last grid size; a change triggers a rebuild (handles resize)
        self._heads: list[float] = []  # per-column streak head row (fractional for varied speed)
        self._lens: list[int] = []
        self._speeds: list[float] = []
        self._buf: list[list[str]] = []  # the glyph each cell currently shows
        self._title_row = 0

    def on_mount(self) -> None:
        self.tooltip = "Click the title to change the model · press b to fold the banner"
        self.set_interval(1 / 15, self._tick)  # ~15fps; negligible cost, paused during ops

    # -- animation state ----------------------------------------------------------------

    def _ensure(self, w: int, h: int) -> None:
        if w == self._w and h == self._h:
            return
        self._w, self._h = w, h
        self._heads = [random.uniform(-h, 0) for _ in range(w)]
        self._lens = [random.randint(3, max(h, 3)) for _ in range(w)]
        self._speeds = [random.uniform(0.25, 0.7) for _ in range(w)]
        self._buf = [[" "] * w for _ in range(h)]

    def _tick(self) -> None:
        if self._paused:
            return
        title = self.app.title or ""
        self._phase = (self._phase + 1) % max(len(title) + _SHIMMER_GAP, 1)
        if self._expanded:
            w, h = self.size.width, self.size.height
            if w > 0 and h > 1:
                self._ensure(w, h)
                for c in range(w):
                    prev = int(self._heads[c])
                    self._heads[c] += self._speeds[c]
                    cur = int(self._heads[c])
                    for rr in range(prev + 1, cur + 1):  # the head writes a new glyph as it falls
                        if 0 <= rr < h:
                            self._buf[rr][c] = random.choice(_RAIN_GLYPHS)
                    if cur - self._lens[c] > h:  # streak fully off the bottom -> respawn at top
                        self._heads[c] = random.uniform(-h, -1.0)
                        self._lens[c] = random.randint(3, max(h, 3))
                        self._speeds[c] = random.uniform(0.25, 0.7)
        self.refresh()

    def pause(self, paused: bool) -> None:
        """Freeze/resume the animation — called around long operations so the rain doesn't
        churn (and steal attention) while real work is happening."""
        self._paused = paused

    def toggle(self) -> None:
        self._expanded = not self._expanded
        self.set_class(not self._expanded, "-folded")
        self.refresh()

    # -- rendering ----------------------------------------------------------------------

    def _compact(self, w: int) -> Text:
        """Folded view: just the shimmering title + model on one centred line (no rain)."""
        title, sub = self.app.title or "", self.app.sub_title or ""
        line = Text(no_wrap=True)
        for i, ch in enumerate(title):
            line.append(ch, style=_shimmer_style(self._phase - i))
        if sub:
            line.append("   ")
            line.append(sub, style="dim #7a8a99")
        out = Text(" " * max(0, (w - line.cell_len) // 2))
        out.append_text(line)
        return out

    def render(self) -> Text:
        w, h = self.size.width, self.size.height
        if w <= 0 or h <= 0:
            return Text("")
        if not self._expanded or h <= 1:
            return self._compact(w)
        self._ensure(w, h)

        chars = [[" "] * w for _ in range(h)]
        styles: list[list[str]] = [[""] * w for _ in range(h)]
        for c in range(w):
            head = int(self._heads[c])
            for r in range(h):
                d = head - r
                if 0 <= d < self._lens[c]:
                    g = self._buf[r][c]
                    chars[r][c] = g if g != " " else random.choice(_RAIN_GLYPHS)
                    styles[r][c] = _rain_style(d, self._lens[c])

        # Overlay the title (shimmering) and model, centred — these cells override the rain so
        # they stay readable.
        self._title_row = h // 2
        title, sub = self.app.title or "", self.app.sub_title or ""
        ts = max(0, (w - len(title)) // 2)
        for i, ch in enumerate(title):
            if ts + i < w:
                chars[self._title_row][ts + i] = ch
                styles[self._title_row][ts + i] = _shimmer_style(self._phase - i)
        if sub and self._title_row + 1 < h:
            ms = max(0, (w - len(sub)) // 2)
            for i, ch in enumerate(sub):
                if ms + i < w:
                    chars[self._title_row + 1][ms + i] = ch
                    styles[self._title_row + 1][ms + i] = "bold #cdd9e0"

        text = Text(no_wrap=True)
        for r in range(h):
            for c in range(w):
                text.append(chars[r][c], style=styles[r][c] or None)
            if r < h - 1:
                text.append("\n")
        return text

    def on_click(self) -> None:
        # Clicking the header opens the model picker — same metaphor as the old title bar.
        self.app.action_choose_model()


class ModelScreen(ModalScreen[dict | None]):
    """Pick the provider + model the toolkit talks to. The header shows the active one;
    clicking it (or pressing m) opens this. Switching is BYO-key: a backend with no key
    configured is rejected with a message rather than silently failing later."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, current_backend: str, current_model: str) -> None:
        super().__init__()
        self._backend = current_backend
        self._model = current_model

    def compose(self) -> ComposeResult:
        with Vertical(id="model-picker"):
            yield Label("◢ select provider & model", id="model-title")
            yield Label("provider", classes="wizard-label")
            with RadioSet(id="backend"):
                for b in _BACKENDS:
                    yield RadioButton(b, value=(b == self._backend))
            yield Label("model (blank = provider default)", classes="wizard-label")
            yield Input(
                value=self._model,
                placeholder="e.g. qwen-plus · claude-opus-4-8 · gpt-4.1",
                id="model-name",
            )
            with Horizontal(id="model-buttons"):
                yield Button("Use", variant="primary", id="use")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#model-name", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        else:
            self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def _submit(self) -> None:
        idx = self.query_one("#backend", RadioSet).pressed_index
        backend = _BACKENDS[idx] if idx >= 0 else None
        model = self.query_one("#model-name", Input).value.strip() or None
        self.dismiss({"backend": backend, "model": model})

    def action_cancel(self) -> None:
        self.dismiss(None)


class EditorScreen(ModalScreen[list[str] | None]):
    """Pick which editor to open the run folder in. Editors that aren't installed are shown
    disabled so it's clear what's supported. Dismisses with the chosen launcher argv prefix."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, options: list[tuple[str, list[str] | None]]) -> None:
        super().__init__()
        self._options = options

    def compose(self) -> ComposeResult:
        with Vertical(id="editor-picker"):
            yield Label("◢ open run folder in…", id="editor-title")
            for i, (label, launch) in enumerate(self._options):
                btn = Button(
                    label if launch else f"{label} — not found",
                    id=f"ed-{i}",
                    variant="primary" if launch else "default",
                )
                btn.disabled = launch is None
                yield btn
            yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        idx = int(event.button.id.removeprefix("ed-"))
        self.dismiss(self._options[idx][1])

    def action_cancel(self) -> None:
        self.dismiss(None)


# The sources the wizard offers, in display order: (key, radio label, origin placeholder).
# `key` is what run_discover dispatches on. The web/API surfaces lead; the backend surfaces
# (events, databases) follow. The placeholder retitles the single location field per source.
_WIZARD_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("openapi", "OpenAPI / Swagger spec  (file or URL)", "https://api.example.com/openapi.json"),
    ("crawl", "Crawl a running web app  (URL)", "https://app.example.com"),
    ("asyncapi", "AsyncAPI spec  (file or URL)", "./asyncapi.yaml  or  https://…/asyncapi.json"),
    ("registry", "Schema registry  (live URL)", "http://localhost:8081"),
    ("db", "Database  (connection URL or .sql DDL)", "postgresql://user@host/db   or   ./schema.sql"),
)


class WizardScreen(ModalScreen[dict | None]):
    """Guided onboarding for a new system — replaces having to know commands."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, default_model: str = "") -> None:
        super().__init__()
        self._default_model = default_model

    def compose(self) -> ComposeResult:
        with Vertical(id="wizard"):
            yield Label("◢ discover a new system", id="wizard-title")
            yield Label("source", classes="wizard-label")
            with RadioSet(id="source"):
                for i, (_key, label, _ph) in enumerate(_WIZARD_SOURCES):
                    yield RadioButton(label, value=(i == 0))
            yield Label("location", classes="wizard-label")
            yield Input(placeholder=_WIZARD_SOURCES[0][2], id="origin")
            yield Label("model (blank = configured default)", classes="wizard-label")
            yield Input(value=self._default_model, placeholder="e.g. qwen3-max", id="model")
            with Horizontal(id="wizard-buttons"):
                yield Button("Discover", variant="primary", id="go")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#origin", Input).focus()

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        # Retitle the location field's placeholder to match the chosen source (URL vs file vs
        # connection string), so it's obvious what to paste.
        idx = event.radio_set.pressed_index
        if 0 <= idx < len(_WIZARD_SOURCES):
            self.query_one("#origin", Input).placeholder = _WIZARD_SOURCES[idx][2]

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        else:
            self._submit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def _submit(self) -> None:
        origin = self.query_one("#origin", Input).value.strip()
        if not origin:
            self.notify("Enter a spec/app URL, connection string, or file path.", severity="warning")
            return
        idx = self.query_one("#source", RadioSet).pressed_index
        source = _WIZARD_SOURCES[idx][0] if 0 <= idx < len(_WIZARD_SOURCES) else "openapi"
        model = self.query_one("#model", Input).value.strip() or None
        self.dismiss({"source": source, "origin": origin, "model": model})

    def action_cancel(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen):
    """Keybinding + workflow reference. Any key closes it."""

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help"):
            yield Static(Text.from_markup(_HELP))

    def on_key(self, event) -> None:
        self.dismiss()

    def on_click(self, event) -> None:
        self.dismiss()


class ConfirmScreen(ModalScreen[bool]):
    """Yes/No confirmation for consequential actions. The confirm button's label/variant are
    caller-supplied so it reads correctly per action (e.g. 'Delete' vs 'Enable')."""

    BINDINGS = [Binding("escape", "no", "Cancel")]

    def __init__(
        self, message: str, *, confirm_label: str = "Confirm", confirm_variant: str = "error"
    ) -> None:
        super().__init__()
        self._message = message
        self._confirm_label = confirm_label
        self._confirm_variant = confirm_variant

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm"):
            yield Label(self._message, id="confirm-message")
            with Horizontal(id="confirm-buttons"):
                yield Button(self._confirm_label, variant=self._confirm_variant, id="yes")
                yield Button("Cancel", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_no(self) -> None:
        self.dismiss(False)


class ResultsScreen(ModalScreen[bool]):
    """Shows the pytest run summary (failures + reasons) front-and-centre. When the run had
    failures, offers [f] to self-heal them — dismisses True so the app kicks off a fix."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("q", "close", "Close"),
        Binding("f", "fix", "Fix failing"),
    ]

    def __init__(self, title: str, text: str, *, has_failures: bool = False) -> None:
        super().__init__()
        self._title = title
        self._text = text
        self._has_failures = has_failures

    def compose(self) -> ComposeResult:
        hint = "[b]f[/] fix failing · esc / q close" if self._has_failures else "esc / q to close"
        with Vertical(id="results"):
            yield Label(self._title, id="results-title")
            with VerticalScroll(id="results-body"):
                yield Static(Text(self._text or "(no output)"))
            yield Label(Text.from_markup(hint), id="results-hint")

    def on_mount(self) -> None:
        self.query_one("#results-body").focus()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "fix":
            return self._has_failures  # hide [f] when nothing failed
        return True

    def action_close(self) -> None:
        self.dismiss(False)

    def action_fix(self) -> None:
        if self._has_failures:
            self.dismiss(True)


class WorkbenchCommands(Provider):
    """Power-user command palette (Ctrl+P) — actions, not memorised slash verbs."""

    async def search(self, query: str) -> Hits:
        app = self.app
        matcher = self.matcher(query)
        commands = [
            ("New system", app.action_new_system),
            ("Scaffold current system", app.action_scaffold),
            ("Write tests for current system", app.action_write),
            ("Re-discover current system", app.action_rediscover),
            ("Run tests for current system", app.action_run_tests),
            ("Fix failing tests (self-heal)", app.action_fix_failing),
            ("Enable selected (skipped) draft", app.action_enable_test),
            ("Open run folder in editor", app.action_open_editor),
            ("Change provider / model", app.action_choose_model),
            ("Delete current system", app.action_delete),
            ("Toggle live log", app.action_toggle_log),
        ]
        for name, runnable in commands:
            score = matcher.match(name)
            if score > 0:
                yield Hit(score, matcher.highlight(name), runnable)


class AitomationApp(App):
    CSS = """
    Screen { layers: base; }
    #main { height: 1fr; }
    #systems { width: 40; border-right: solid $primary; }
    #tabs { width: 1fr; }
    #log { height: 9; border-top: solid $primary; background: $surface; padding: 0 1; }
    #log.-hidden { display: none; }
    #statusbar { height: 1; background: $panel; }
    #progress { width: 32; display: none; padding: 0 1; }
    #status { width: 1fr; padding: 0 1; color: $accent; }
    DataTable { height: 1fr; background: $surface; }
    .detail { height: auto; max-height: 14; padding: 1; border-top: solid $primary; }
    HeaderTitle:hover { background: $foreground 10%; }
    #wizard { width: 64; height: auto; padding: 1 2; background: $surface; border: round $primary; }
    #wizard-title { color: $accent; text-style: bold; width: 100%; content-align: center middle; padding-bottom: 1; }
    #model-picker { width: 60; height: auto; padding: 1 2; background: $surface; border: round $primary; }
    #model-title { color: $accent; text-style: bold; width: 100%; content-align: center middle; padding-bottom: 1; }
    #model-buttons { height: auto; padding-top: 1; align: center middle; }
    #model-buttons Button { margin: 0 1; }
    #editor-picker { width: 48; height: auto; padding: 1 2; background: $surface; border: round $primary; }
    #editor-title { color: $accent; text-style: bold; width: 100%; content-align: center middle; padding-bottom: 1; }
    #editor-picker Button { width: 100%; margin: 0 0 1 0; }
    .wizard-label { color: $secondary; padding-top: 1; }
    #wizard-buttons { height: auto; padding-top: 1; align: center middle; }
    #wizard-buttons Button { margin: 0 1; }
    #help { width: 74; height: auto; max-height: 90%; padding: 1 2; background: $surface; border: round $primary; }
    #confirm { width: 56; height: auto; padding: 1 2; background: $surface; border: round $error; }
    #confirm-message { width: 100%; padding-bottom: 1; }
    #confirm-buttons { height: auto; align: center middle; }
    #confirm-buttons Button { margin: 0 1; }
    #results { width: 86%; height: 82%; padding: 1 2; background: $surface; border: round $primary; }
    #results-title { text-style: bold; color: $accent; padding-bottom: 1; }
    #results-body { height: 1fr; }
    #results-hint { color: $text-muted; padding-top: 1; }
    """

    BINDINGS = [
        Binding("n", "new_system", "New"),
        Binding("s", "scaffold", "Scaffold"),
        Binding("w", "write", "Write"),
        Binding("r", "rediscover", "Re-discover"),
        Binding("t", "run_tests", "Run"),
        Binding("f", "fix_failing", "Fix"),
        Binding("e", "enable_test", "Enable"),
        Binding("o", "open_editor", "Open"),
        Binding("d", "delete", "Delete"),
        Binding("l", "toggle_log", "Log"),
        Binding("b", "toggle_banner", "Banner"),
        Binding("m", "choose_model", "Model"),
        Binding("question_mark", "help", "Help"),
        Binding("q", "quit", "Quit"),
    ]
    COMMANDS = App.COMMANDS | {WorkbenchCommands}
    TITLE = "Aitomation"

    def __init__(
        self,
        *,
        llm: LLMProvider | None = None,
        provider_override: str | None = None,
        model_override: str | None = None,
        usage_log: str | Path = DEFAULT_LOG,
        workspace_root: str | Path | None = None,
    ) -> None:
        super().__init__()
        # Default workspace lives under projects/ so generated systems don't litter the repo
        # root. Each system gets projects/<slug>/ (see Workspace), shared with the CLI.
        self.workspace = Workspace(workspace_root if workspace_root is not None else PROJECTS_ROOT)
        self.recorder = UsageRecorder(app="tui-session", log_path=usage_log)
        self._injected_llm = llm
        self._llm: LLMProvider | None = llm
        self._provider_override = provider_override
        self._model_override = model_override
        self._config: LLMConfig | None = None
        self._records: list[SystemRecord] = []
        self.current: SystemRecord | None = None
        self.current_inv = None
        self._elements: list = []
        self._journeys: list = []
        self._test_files: list[tuple[str, str, Path]] = []
        # Per-file outcome from the latest pytest run/fix ('passed'/'failed'/'skipped'), keyed
        # by file name. Overlaid onto the Tests tab so the status reflects the last RUN, not
        # just static file markers. Reset when the selected system changes.
        self._test_outcomes: dict[str, str] = {}
        self._last_run_failed = False  # gates the [f] fix affordance

    # -- layout -------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield MatrixBanner(id="banner")
        with Horizontal(id="main"):
            systems = DataTable(id="systems", cursor_type="row")
            systems.border_title = "systems"
            yield systems
            with TabbedContent(id="tabs"):
                with TabPane("Overview", id="tab-overview"):
                    yield VerticalScroll(Static(id="overview"))
                with TabPane("Coverage", id="tab-surface"):
                    with Vertical():
                        yield DataTable(id="surface", cursor_type="row")
                        yield Static(id="surface-detail", classes="detail")
                with TabPane("Flows", id="tab-journeys"):
                    with Vertical():
                        yield DataTable(id="journeys", cursor_type="row")
                        yield Static(id="journeys-detail", classes="detail")
                with TabPane("Tests", id="tab-tests"):
                    with Vertical():
                        yield DataTable(id="tests", cursor_type="row")
                        yield VerticalScroll(Static(id="tests-detail"), classes="detail")
                with TabPane("Usage", id="tab-usage"):
                    yield VerticalScroll(Static(id="usage"))
        log = RichLog(id="log", markup=True, highlight=False, wrap=True)
        log.border_title = "live log"
        yield log
        with Horizontal(id="statusbar"):
            yield ProgressBar(id="progress", show_eta=False)
            yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(CYBERPUNK)
        self.theme = "cyberpunk"
        self.query_one("#systems", DataTable).add_column("systems")
        surface = self.query_one("#surface", DataTable)
        surface.add_column("kind", width=8)
        surface.add_column("name", width=22)
        surface.add_column("location", width=26)
        surface.add_column("pri", width=6)
        journeys = self.query_one("#journeys", DataTable)
        journeys.add_column("pri", width=6)
        journeys.add_column("name", width=26)
        journeys.add_column("steps", width=6)
        journeys.add_column("touches")
        tests = self.query_one("#tests", DataTable)
        tests.add_column("file", width=36)
        tests.add_column("status")
        if self._injected_llm is None:
            self._resolve_provider()
        self._log("workbench ready")
        self._refresh_systems(select=0)

    # -- provider -----------------------------------------------------------------------

    def _resolve_provider(self) -> None:
        try:
            self._config = LLMConfig.from_env(
                backend=self._provider_override, model=self._model_override
            )
            self._llm = PydanticAIProvider(self._config, self.recorder)
            self.sub_title = f"{self._config.backend}:{self._config.model}"
        except ConfigError:
            self.sub_title = "no LLM key"
            self._log("[#ffd400]no LLM configured[/] — browse/scaffold work; discover/write need a key")

    def _provider_ready(self) -> bool:
        if self._llm is None:
            self.notify("No LLM configured. Set a provider key (see README).", severity="error")
            return False
        return True

    def _model(self) -> str | None:
        return self._model_override or (self._config.model if self._config else None)

    def action_choose_model(self) -> None:
        """Open the provider/model picker (also reachable by clicking the title bar)."""
        backend = self._config.backend if self._config else (self._provider_override or "anthropic")
        model = self._config.model if self._config else (self._model_override or "")
        self.push_screen(ModelScreen(backend, model), self._on_model_chosen)

    def _on_model_chosen(self, result: dict | None) -> None:
        if result:
            self._apply_model_choice(result["backend"], result["model"])

    def _apply_model_choice(self, backend: str | None, model: str | None) -> None:
        """Re-resolve the provider for the chosen backend/model. BYO-key: a backend with no
        key configured is rejected here (with a message) instead of failing mid-operation."""
        try:
            cfg = LLMConfig.from_env(backend=backend, model=model)
        except ConfigError as e:
            self.notify(f"Can't switch: {e}", severity="error", timeout=8)
            return
        self._provider_override = backend
        self._model_override = model
        self._config = cfg
        self._llm = PydanticAIProvider(cfg, self.recorder)
        self.sub_title = f"{cfg.backend}:{cfg.model}"
        self._log(f"[#39ff14]model[/] → {cfg.backend}:{cfg.model} [dim](output: {cfg.output_mode})[/]")

    # -- log + status -------------------------------------------------------------------

    def _log(self, msg: str, *, status: bool = True) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.query_one("#log", RichLog).write(f"[dim]{ts}[/] {msg}")
        if status:
            self.query_one("#status", Static).update(msg)

    def _set_banner_paused(self, paused: bool) -> None:
        # Freeze the header animation around long operations; tolerate the banner being absent.
        try:
            self.query_one(MatrixBanner).pause(paused)
        except Exception:  # noqa: BLE001 — purely cosmetic, never block an op on it
            pass

    def _begin_progress(self, total: int | None, label: str) -> None:
        bar = self.query_one("#progress", ProgressBar)
        bar.display = True
        bar.update(total=total, progress=0)
        self._set_banner_paused(True)
        self._log(label)

    def _advance_progress(self, n: int = 1) -> None:
        self.query_one("#progress", ProgressBar).advance(n)

    def _end_progress(self, label: str = "") -> None:
        self.query_one("#progress", ProgressBar).display = False
        self._set_banner_paused(False)
        if label:
            self._log(label)

    def action_toggle_log(self) -> None:
        self.query_one("#log", RichLog).toggle_class("-hidden")

    def action_toggle_banner(self) -> None:
        self.query_one(MatrixBanner).toggle()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # Hide [f] until a run has actually failed — fix only makes sense on failures.
        if action == "fix_failing":
            return self._last_run_failed
        # Hide [e] unless the highlighted draft is skipped — enable only makes sense then.
        if action == "enable_test":
            try:
                sel = self._selected_test()
            except Exception:  # noqa: BLE001 — DOM may not be ready during early mount
                return False
            return bool(sel and "skip" in sel[1])
        return True

    # -- systems library ----------------------------------------------------------------

    @staticmethod
    def _rail_label(rec: SystemRecord) -> Text:
        t = Text()
        t.append("●", style="#22d3ee")
        t.append("●" if rec.scaffolded else "○", style="#22d3ee" if rec.scaffolded else "#39455c")
        t.append("●" if rec.drafted else "○", style="#22d3ee" if rec.drafted else "#39455c")
        t.append("  ")
        t.append(rec.name)
        return t

    def _refresh_systems(self, select: int | None = None) -> None:
        table = self.query_one("#systems", DataTable)
        table.clear()
        self._records = self.workspace.list_systems()
        for rec in self._records:
            table.add_row(self._rail_label(rec))
        if not self._records:
            self.current = None
            self.current_inv = None
            self._render_overview_empty()
            return
        if select is not None:
            row = min(select, len(self._records) - 1)
            table.move_cursor(row=row)
            self._select_system(row)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        tid = event.data_table.id
        if tid == "systems":
            self._select_system(event.cursor_row)
        elif tid == "surface":
            self._show_element(event.cursor_row)
        elif tid == "journeys":
            self._show_journey(event.cursor_row)
        elif tid == "tests":
            self._show_test(event.cursor_row)
            self.refresh_bindings()  # reveal/hide [e] depending on whether this draft is skipped

    def _select_system(self, idx: int) -> None:
        if not (0 <= idx < len(self._records)):
            return
        self.current = self._records[idx]
        self.current_inv = self.workspace.load_inventory(self.current.slug)
        # Rehydrate the latest run's per-file outcomes from disk so the Tests-tab status
        # survives a restart / re-selection (per-system; never bled across selections).
        run = self.current.latest_run
        self._test_outcomes = self._load_outcomes(Path(run)) if run else {}
        self._populate_tabs()

    def _load_outcomes(self, run: Path) -> dict[str, str]:
        """The latest run's per-file pass/fail, read back from disk so the Tests-tab status
        SURVIVES a restart instead of resetting to static file markers. Prefers the small
        status file we write after each run/fix; falls back to parsing the persisted pytest
        output (so runs recorded before this existed still light up). This is the last run's
        view kept next to pytest-output.txt — not a cross-run results store."""
        status = run / _STATUS_FILE
        if status.is_file():
            try:
                data = json.loads(status.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return {str(k): str(v) for k, v in data.items()}
            except (json.JSONDecodeError, OSError):
                pass
        out = run / "pytest-output.txt"
        if out.is_file():
            try:
                return _parse_pytest_outcomes(out.read_text(encoding="utf-8").splitlines())
            except OSError:
                pass
        return {}

    def _save_outcomes(self, run: Path) -> None:
        """Persist the current per-file outcomes next to pytest-output.txt (see _load_outcomes)."""
        try:
            (run / _STATUS_FILE).write_text(json.dumps(self._test_outcomes), encoding="utf-8")
        except OSError:
            pass

    def _current_index(self) -> int:
        for i, r in enumerate(self.workspace.list_systems()):
            if self.current and r.slug == self.current.slug:
                return i
        return 0

    # -- tab population -----------------------------------------------------------------

    def _populate_tabs(self) -> None:
        self._render_overview()
        self._render_surface()
        self._render_journeys()
        self._render_tests()
        self._render_usage()

    def _render_overview_empty(self) -> None:
        self.query_one("#overview", Static).update(
            Text.from_markup(
                "[b]No systems yet.[/]\n\nPress [b]n[/] to discover one "
                "(OpenAPI spec/URL or a running web app)."
            )
        )
        for tid in ("surface", "journeys", "tests"):
            self.query_one(f"#{tid}", DataTable).clear()
        for did in ("surface-detail", "journeys-detail", "tests-detail", "usage"):
            self.query_one(f"#{did}", Static).update("")

    @staticmethod
    def _next_hint(rec: SystemRecord) -> str:
        if not rec.scaffolded:
            return "[#39ff14]▸ next[/] press [b]s[/] to scaffold a runnable project"
        if not rec.drafted:
            return "[#39ff14]▸ next[/] press [b]w[/] to draft tests, one per flow"
        return "[#39ff14]▸ next[/] review drafts in the [b]Tests[/] tab · [b]r[/] to re-discover"

    def _cost_for(self, name: str) -> dict:
        recs = [r for r in load_records(self.recorder.log_path) if r.get("app") == name]
        recs += [r.to_dict() for r in self.recorder.records if r.app == name]
        disc = sum(r["total_tokens"] for r in recs if str(r["label"]).startswith("discover"))
        writes = [r for r in recs if str(r["label"]).startswith("write:")]
        n = len(writes)
        wt = sum(r["total_tokens"] for r in writes)
        return {"any": bool(recs), "discover": disc, "n_tests": n, "write_total": wt,
                "avg": round(wt / n) if n else 0}

    def _render_overview(self) -> None:
        inv, rec = self.current_inv, self.current
        if inv is None or rec is None:
            return
        auth = inv.auth_strategy or "none"
        if inv.auth_schemes:
            s = inv.auth_schemes[0]
            detail = s.name or s.scheme or s.type
            if detail and detail.lower() != auth.lower():
                auth = f"{auth} ({detail})"
        kinds = ", ".join(f"{k}={v}" for k, v in sorted(inv.counts_by_kind().items()))
        prios = ", ".join(f"{k}={v}" for k, v in sorted(inv.counts_by_priority().items()))
        body = Text()
        body.append(f"{inv.system_name}\n", style="bold #22d3ee")
        body.append(f"{inv.base_url}\n", style="dim")
        body.append(f"\nsource    {inv.source}\n")
        body.append(f"auth      {auth}\n")
        # pipeline dots with the next step called out underneath
        body.append("pipeline  ")
        for label, on in [("discover", True), ("scaffold", rec.scaffolded), ("write", rec.drafted)]:
            body.append("● " if on else "○ ", style="#22d3ee" if on else "#39455c")
            body.append(label + "   ", style="#cde7f0" if on else "#39455c")
        body.append("\n          ")
        body.append_text(Text.from_markup(self._next_hint(rec)))
        body.append("\n")
        body.append(f"coverage  {len(inv.elements)} elements ({kinds})\n")
        body.append(f"priority  {prios}\n")
        body.append(f"flows     {len(inv.suggested_journeys)}\n")
        cost = self._cost_for(inv.system_name)
        if cost["any"]:
            body.append("\ncost\n", style="bold #f637ec")
            body.append(f"  discover  ~{cost['discover']:,} tok\n")
            if cost["n_tests"]:
                body.append(
                    f"  tests     {cost['n_tests']} drafted · ~{cost['avg']:,} tok/test"
                    f" · suite ~{cost['write_total']:,} tok\n"
                )
        # action bar — green ✓ once a stage is done (kept above the fold)
        body.append("\nactions   ")
        body.append("[ s ] scaffold ", style="dim")
        body.append("✓" if rec.scaffolded else "·", style="#39ff14" if rec.scaffolded else "#39455c")
        body.append("   [ w ] write tests ", style="dim")
        body.append("✓" if rec.drafted else "·", style="#39ff14" if rec.drafted else "#39455c")
        body.append("   [ r ] re-discover\n", style="dim")

        if rec.scaffolded and rec.latest_run:
            body.append("\nrun       ", style="bold #f637ec")
            body.append("[ t ] run pytest here   [ o ] open in editor\n", style="dim")
            body.append(f"  cd {rec.latest_run}\n", style="dim")
            body.append("  uv sync && uv run playwright install chromium && uv run pytest -ra\n", style="dim")
            body.append("  # containerised:  docker build -t e2e . && docker run --rm e2e\n", style="dim")
            body.append(f"\noutput    {rec.latest_run}\n", style="dim")
        if inv.summary:
            body.append(f"\n{inv.summary}\n")
        self.query_one("#overview", Static).update(body)

    def _render_surface(self) -> None:
        table = self.query_one("#surface", DataTable)
        table.clear()
        self._elements = list(self.current_inv.elements)
        for e in self._elements:
            loc = f"{e.method + ' ' if e.method else ''}{e.location}"
            table.add_row(e.kind, e.name, loc, e.priority)
        self.query_one("#surface-detail", Static).update("")

    def _show_element(self, idx: int) -> None:
        if not (0 <= idx < len(self._elements)):
            return
        e = self._elements[idx]
        body = Text()
        body.append(f"{e.name}", style="bold #22d3ee")
        body.append(f"  {e.method or ''} {e.location}\n", style="dim")
        body.append(f"{e.description}\n")
        if e.preconditions:
            body.append(f"\npreconditions: {', '.join(e.preconditions)}\n", style="dim")
        if e.inputs:
            body.append("\ninputs:\n", style="dim")
            for i in e.inputs:
                ex = f"  e.g. {i.example}" if i.example else ""
                req = "*" if i.required else ""
                body.append(f"  {i.name}{req} ({i.where}:{i.type}){ex}\n")
        self.query_one("#surface-detail", Static).update(body)

    def _render_journeys(self) -> None:
        table = self.query_one("#journeys", DataTable)
        table.clear()
        self._journeys = list(self.current_inv.suggested_journeys)
        for j in self._journeys:
            touches = ", ".join(j.elements[:3]) + ("…" if len(j.elements) > 3 else "")
            table.add_row(j.priority, j.name, str(len(j.steps)), touches or "—")
        self.query_one("#journeys-detail", Static).update("")

    def _show_journey(self, idx: int) -> None:
        if not (0 <= idx < len(self._journeys)):
            return
        j = self._journeys[idx]
        body = Text()
        body.append(f"{j.name}", style="bold #22d3ee")
        body.append(f"  [{j.priority}]\n", style="dim")
        body.append(f"{j.description}\n")
        if j.steps:
            body.append("\nsteps:\n", style="dim")
            for n, s in enumerate(j.steps, 1):
                body.append(f"  {n}. {s.action}\n")
        if j.elements:
            body.append(f"\ntouches: {', '.join(j.elements)}\n", style="dim")
        self.query_one("#journeys-detail", Static).update(body)

    def _scan_tests(self) -> list[tuple[str, str, Path]]:
        if self.current is None or not self.current.latest_run:
            return []
        run = Path(self.current.latest_run)
        out: list[tuple[str, str, Path]] = []
        tests_dir = run / "tests"
        if tests_dir.is_dir():
            for p in sorted(tests_dir.glob("test_*.py")):
                text = p.read_text(encoding="utf-8")
                outcome = self._test_outcomes.get(p.name)
                if "mark.skip" in text and "DESTRUCTIVE" in text:
                    status = "skipped · destructive"
                elif "mark.skip" in text:
                    status = "skipped"
                # The latest RUN wins over static file markers, so a fixed+passing test stops
                # reading as "failing" (and a now-broken one stops reading as "ok").
                elif outcome == "failed":
                    status = "failed"
                elif outcome == "passed":
                    status = "passed"
                elif outcome == "skipped":
                    status = "skipped"
                elif "RUNTIME FAILURE" in text:
                    status = "failing · see notes"
                else:
                    status = "ok"
                out.append((p.name, status, p))
        review = run / "drafts_needs_review"
        if review.is_dir():
            for p in sorted(review.glob("*.py.txt")):
                out.append((p.name, "needs review", p))
        return out

    def _render_tests(self) -> None:
        table = self.query_one("#tests", DataTable)
        table.clear()
        self._test_files = self._scan_tests()
        for name, status, _ in self._test_files:
            table.add_row(name, _status_text(status))
        if not self._test_files:
            self.query_one("#tests-detail", Static).update(
                Text("No drafts yet. Scaffold (s), then write tests (w).", style="dim")
            )

    def _show_test(self, idx: int) -> None:
        if not (0 <= idx < len(self._test_files)):
            return
        code = self._test_files[idx][2].read_text(encoding="utf-8")
        self.query_one("#tests-detail", Static).update(
            Syntax(code, "python", theme="ansi_dark", word_wrap=True)
        )

    def _render_usage(self) -> None:
        records = [r for r in load_records(self.recorder.log_path) if r.get("app") == self.current.name]
        records += [r.to_dict() for r in self.recorder.records if r.app == self.current.name]
        if not records:
            self.query_one("#usage", Static).update(Text("No LLM usage recorded for this system yet.", style="dim"))
            return
        table = RichTable(expand=True)
        for col in ("prompt", "calls", "in", "out", "total", "sec"):
            table.add_column(col, justify="left" if col == "prompt" else "right")
        for g in aggregate(records, ("label",)):
            table.add_row(g["label"], str(g["calls"]), f"{g['input_tokens']:,}",
                          f"{g['output_tokens']:,}", f"{g['total_tokens']:,}", f"{g['duration_s']}")
        total = aggregate(records, ())[0]  # overall total across all prompts
        table.add_section()
        table.add_row("TOTAL", str(total["calls"]), f"{total['input_tokens']:,}",
                      f"{total['output_tokens']:,}", f"{total['total_tokens']:,}",
                      f"{total['duration_s']}", style="bold #39ff14")
        self.query_one("#usage", Static).update(table)

    # -- actions ------------------------------------------------------------------------

    def action_new_system(self) -> None:
        self.push_screen(WizardScreen(self._model() or ""), self._on_wizard)

    def _on_wizard(self, result: dict | None) -> None:
        if not result or not self._provider_ready():
            return
        self.run_discover(result["source"], result["origin"], result.get("model"))

    def action_rediscover(self) -> None:
        if self.current is None:
            self.notify("Select a system first.", severity="warning")
            return
        if self._provider_ready():
            self.run_discover(self.current.source, self.current.origin, self._model())

    def action_scaffold(self) -> None:
        if self.current is None or self.current_inv is None:
            self.notify("Select a system first.", severity="warning")
            return
        # Refresh an existing run in place (Copier overwrites framework files but keeps the
        # drafted tests/), so re-scaffolding after a re-discover never orphans prior work.
        run = (
            Path(self.current.latest_run)
            if self.current.latest_run
            else self.workspace.new_run(self.current.slug)
        )
        try:
            scaffold_project(self.current_inv, run)
        except Exception as e:  # noqa: BLE001
            self._log(f"[#ff2e63]scaffold failed[/] {escape(str(e))}")
            self.notify(f"Scaffold failed: {e}", severity="error")
            return
        self.workspace.set_flags(self.current.slug, scaffolded=True, latest_run=str(run))
        self._log(f"[#39ff14]scaffolded[/] → {run} · next: write tests ([b]w[/])")
        self._refresh_systems(select=self._current_index())
        self.notify("Scaffolded. Press w to draft tests.")

    def action_write(self) -> None:
        if self.current is None or self.current_inv is None:
            self.notify("Select a system first.", severity="warning")
            return
        if not self.current.scaffolded or not self.current.latest_run:
            self.notify("Scaffold first (s) so drafts are runnable.", severity="warning")
            return
        if self._provider_ready():
            self.run_write()

    def action_delete(self) -> None:
        if self.current is None:
            self.notify("Select a system first.", severity="warning")
            return
        self.push_screen(
            ConfirmScreen(
                f"Delete '{self.current.name}' and its generated runs?", confirm_label="Delete"
            ),
            self._on_delete_confirm,
        )

    def _on_delete_confirm(self, ok: bool) -> None:
        if not ok or self.current is None:
            return
        name = self.current.name
        self.workspace.delete(self.current.slug)
        self._log(f"deleted {name}")
        self._refresh_systems(select=0)

    def action_run_tests(self) -> None:
        if self.current is None or not self.current.scaffolded or not self.current.latest_run:
            self.notify("Scaffold first (s) so there are tests to run.", severity="warning")
            return
        self.run_tests()

    def action_fix_failing(self) -> None:
        if self.current is None or not self.current.latest_run:
            self.notify("Nothing to fix — scaffold (s) and run tests (t) first.", severity="warning")
            return
        if not self._last_run_failed:
            self.notify("Nothing to fix — run tests (t) first; fix targets failures.", severity="warning")
            return
        if self._provider_ready():
            self.run_fix()

    def _selected_test(self) -> tuple[str, str, Path] | None:
        """The (name, status, path) of the highlighted row in the Tests panel, or None."""
        if not self._test_files:
            return None
        row = self.query_one("#tests", DataTable).cursor_row
        if row is None or not (0 <= row < len(self._test_files)):
            return None
        return self._test_files[row]

    def action_enable_test(self) -> None:
        """Lift the destructive-skip guard on the highlighted draft (skipped → ok). No LLM."""
        if self.current is None or not self.current.latest_run:
            self.notify("Nothing to enable — scaffold (s) and write (w) first.", severity="warning")
            return
        sel = self._selected_test()
        if sel is None:
            self.notify("Highlight a test in the Tests tab first.", severity="warning")
            return
        name, status, _path = sel
        if "skip" not in status:
            self.notify(f"{name} isn't skipped — nothing to enable.", severity="warning")
            return
        self.push_screen(
            ConfirmScreen(
                f"Enable '{name}'? It performs MUTATING requests when run — make sure teardown "
                "is in place first.",
                confirm_label="Enable",
                confirm_variant="warning",
            ),
            self._on_enable_confirm,
        )

    def _on_enable_confirm(self, ok: bool) -> None:
        if not ok or self.current is None or not self.current.latest_run:
            return
        row = self.query_one("#tests", DataTable).cursor_row
        sel = self._selected_test()  # re-read: the highlight can't change while the modal is up
        if sel is None:
            return
        name = sel[0]
        results = enable_drafts(Path(self.current.latest_run), targets=[sel[2].stem])
        if results and results[0].enabled:
            self._log(
                f"[#39ff14]enabled[/] {name} — skip lifted · "
                "verify teardown before running ([b]t[/])"
            )
            self._render_tests()  # status flips skipped → ok
            if row is not None:
                self._show_test(row)  # refresh the source preview (guard now gone)
            self.notify(f"Enabled {name}. It now performs mutating requests when run.")
        else:
            self.notify(f"{name} is already runnable — nothing to enable.")

    def action_open_editor(self) -> None:
        if self.current is None or not self.current.latest_run:
            self.notify("Nothing to open — scaffold first (s).", severity="warning")
            return
        options = [(label, _resolve_editor(cli, apps)) for label, cli, apps in _EDITORS]
        if not any(launch for _, launch in options):
            self.notify(
                "No supported editor found (VS Code / PyCharm / Cursor / Antigravity).",
                severity="warning",
            )
            self._log(f"open manually: {self.current.latest_run}")
            return
        self.push_screen(EditorScreen(options), self._on_editor_chosen)

    def _on_editor_chosen(self, launch: list[str] | None) -> None:
        if not launch or self.current is None or not self.current.latest_run:
            return
        target = self.current.latest_run
        try:
            subprocess.Popen([*launch, target])  # fire-and-forget; opens the folder
        except OSError as e:
            self.notify(f"Couldn't launch editor: {e}", severity="error")
            return
        self._log(f"opened {target} via {' '.join(launch)}")

    def _report_inventory_diff(self, old, new, rec: SystemRecord) -> None:
        """Log what changed vs the previous discover, and flag drafted tests that cover
        changed/added surface (those may need a re-draft with force)."""
        d = diff_inventories(old, new)
        if d.is_empty:
            self._log("[dim]re-discover: no changes since last time[/]")
            return
        self._log(f"[#22d3ee]changes since last discover[/] — {d.summary()}")
        if d.added_journeys:
            names = ", ".join(j.name for j in d.added_journeys)
            self._log(f"  [#39ff14]new flow(s)[/]: {names} — press [b]w[/] to draft them")
        if rec.latest_run and d.affected_journeys:
            tests = Path(rec.latest_run) / "tests"
            stale = [j.name for j in d.affected_journeys if (tests / f"test_{_func_name(j.name)}.py").exists()]
            if stale:
                self._log("  [#ffd400]⚠ existing test(s) may be stale[/]: " + ", ".join(stale))
        self.notify(f"Changes since last discover — {d.summary()}", timeout=8)

    # -- workers ------------------------------------------------------------------------

    @work(exclusive=True, group="op")
    async def run_discover(self, source: str, origin: str, model: str | None) -> None:
        provider = self._llm
        if model and self._config and model != self._config.model:
            try:
                provider = PydanticAIProvider(
                    LLMConfig.from_env(backend=self._config.backend, model=model), self.recorder
                )
            except ConfigError:
                provider = self._llm
        self.recorder.app = origin
        self._begin_progress(None, f"discovering {escape(origin)} …")
        # `source` is a wizard key on a new discover, but re-discover passes the saved
        # inventory's DiscoverySource ('schema_registry'/'db_schema'); normalise both forms.
        src = {"schema_registry": "registry", "db_schema": "db"}.get(source, source)
        try:
            if src == "openapi":
                inv = await discover_openapi(origin, provider)
            elif src == "asyncapi":
                inv = await discover_asyncapi(origin, provider)
            elif src == "registry":
                inv = await discover_registry(origin, provider)
            elif src == "db":
                inv = await discover_db(origin, provider)
            else:  # crawl
                inv = await discover_crawl(
                    origin, provider, on_page=lambda p: self._log(f"crawled {escape(p.url)}")
                )
        except Exception as e:  # noqa: BLE001
            self._log(f"[#ff2e63]discovery failed[/] {escape(str(e))}")
            self.notify(f"Discovery failed: {type(e).__name__}: {e}", severity="error", timeout=8)
            return
        finally:
            self._end_progress()
        # Grab the prior inventory BEFORE save overwrites it, so a re-discover can report
        # what changed (and which drafted tests may now be stale).
        baseline = self.workspace.try_load_inventory(slugify(inv.system_name))
        rec = self.workspace.save(inv, origin=origin)
        self.recorder.app = inv.system_name
        self.recorder.flush()
        self._log(f"[#39ff14]discovered[/] {inv.system_name} — {len(inv.elements)} elements, "
                  f"{len(inv.suggested_journeys)} flows · next: scaffold ([b]s[/])")
        if baseline is not None:
            self._report_inventory_diff(baseline, inv, rec)
        self._refresh_systems(select=0)
        for i, r in enumerate(self._records):
            if r.slug == rec.slug:
                self.query_one("#systems", DataTable).move_cursor(row=i)
                self._select_system(i)
                break

    @work(exclusive=True, group="op")
    async def run_tests(self) -> None:
        run = Path(self.current.latest_run)
        log = self.query_one("#log", RichLog)
        self._log("[#22d3ee]running pytest[/] [dim](the test runner decides pass/fail — the AI never does)[/]")
        self._begin_progress(None, f"pytest in {run.name} …")
        env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
        captured: list[str] = []
        try:
            proc = await asyncio.create_subprocess_exec(
                # -rA: list EVERY test's outcome in the summary so the Tests tab can show
                # per-file pass/fail from this run (not just stale file markers).
                "uv", "run", "pytest", "-rA", "--tb=short", cwd=str(run), env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").rstrip()
                captured.append(line)
                log.write(escape(line))
            rc = await proc.wait()
        except FileNotFoundError:
            self.notify("`uv` not found on PATH.", severity="error")
            return
        finally:
            self._end_progress()

        # Persist the full output, and surface the reasons (not just an exit code).
        (run / "pytest-output.txt").write_text("\n".join(captured), encoding="utf-8")
        summary_at = next((i for i, ln in enumerate(captured) if "short test summary info" in ln), None)
        tail = captured[summary_at:] if summary_at is not None else captured[-25:]
        counts = next(
            (ln.strip("= ") for ln in reversed(captured)
             if " in " in ln and any(w in ln for w in ("passed", "failed", "error"))),
            f"exit {rc}",
        )
        verdict = "[#39ff14]✓[/]" if rc == 0 else "[#ff2e63]✗[/]"
        self._log(f"{verdict} pytest: {counts}  [dim](full output → {run}/pytest-output.txt)[/]")
        self.notify(f"pytest: {counts}", severity="information" if rc == 0 else "warning", timeout=8)
        self._last_run_failed = rc != 0
        self.refresh_bindings()  # reveal/hide [f] in the footer
        # Reflect THIS run's per-test results in the Tests tab status column, and persist them
        # so the status survives a restart (rehydrated on select via _load_outcomes).
        self._test_outcomes = _parse_pytest_outcomes(captured)
        self._save_outcomes(run)
        self._render_tests()
        self.push_screen(
            ResultsScreen(f"pytest — {counts}", "\n".join(tail), has_failures=rc != 0), self._on_results
        )

    def _on_results(self, fix: bool | None) -> None:
        if fix and self._provider_ready():
            self.run_fix()

    @work(exclusive=True, group="op")
    async def run_write(self) -> None:
        dest = Path(self.current.latest_run)
        self.recorder.app = self.current.name
        planned = len(select_journeys(self.current_inv))
        self._begin_progress(planned, f"drafting {planned} flow tests …")

        def progress(r) -> None:
            self._advance_progress()
            if r.confidence == "existing":
                self._log(f"[dim]kept {r.path.name} (already drafted)[/]")
            else:
                self._log(f"drafted {r.path.name} — {'skip (destructive)' if r.destructive else 'ok'}")

        try:
            # Non-destructive: only new flows are drafted; existing tests are kept.
            report = await draft_tests(self.current_inv, self._llm, into=dest, on_draft=progress)
        except Exception as e:  # noqa: BLE001
            self._log(f"[#ff2e63]write failed[/] {escape(str(e))}")
            self.notify(f"Write failed: {type(e).__name__}: {e}", severity="error", timeout=8)
            return
        finally:
            self._end_progress()
        self.workspace.set_flags(self.current.slug, drafted=True)
        self.recorder.flush()
        kept = f" [dim]· {len(report.skipped)} existing kept[/]" if report.skipped else ""
        self._log(f"[#39ff14]drafts written[/]{kept} — review in the Tests tab")
        self._refresh_systems(select=self._current_index())
        self.query_one("#tabs", TabbedContent).active = "tab-tests"

    @work(exclusive=True, group="op")
    async def run_fix(self) -> None:
        inv, dest = self.current_inv, Path(self.current.latest_run)
        self.recorder.app = self.current.name
        self._log("[#22d3ee]fixing[/] [dim]re-running each draft; self-healing the failures (one retry each)[/]")
        self._begin_progress(None, "fixing failing tests …")

        def progress(r) -> None:
            verb = "[#39ff14]fixed[/]" if r.fixed else f"[#ff2e63]still failing[/] [dim]({r.reason})[/]"
            self._log(f"{verb} {r.path.name}")

        try:
            report = await heal_failing_tests(inv, self._llm, into=dest, on_heal=progress)
        except Exception as e:  # noqa: BLE001
            self._log(f"[#ff2e63]fix failed[/] {escape(str(e))}")
            self.notify(f"Fix failed: {type(e).__name__}: {e}", severity="error", timeout=8)
            return
        finally:
            self._end_progress()
        self.recorder.flush()

        n_fixed, n_left = len(report.fixed), len(report.still_failing)
        self._last_run_failed = n_left > 0  # keep [f] available while failures remain
        self.refresh_bindings()
        if not report.fixed and not report.still_failing:
            self._log("[#39ff14]fix[/] nothing to fix — all drafts pass")
            self.notify("Nothing to fix — all drafts pass.")
        else:
            verdict = "[#39ff14]✓[/]" if n_left == 0 else "[#ffd400]∼[/]"
            self._log(f"{verdict} fix: [#39ff14]{n_fixed} fixed[/] · {n_left} still failing")
            self.notify(
                f"Fixed {n_fixed}; {n_left} still failing.",
                severity="information" if n_left == 0 else "warning", timeout=8,
            )
        # Reflect the heal results in the status column: just-fixed tests now read 'passed',
        # ones that still fail read 'failed' — overriding any stale RUNTIME FAILURE marker.
        for r in report.fixed:
            self._test_outcomes[r.path.name] = "passed"
        for r in report.still_failing:
            self._test_outcomes[r.path.name] = "failed"
        self._save_outcomes(dest)  # so the heal result survives a restart too
        self._render_tests()  # refresh per-file status (failing drafts now flagged)
        self.query_one("#tabs", TabbedContent).active = "tab-tests"


def run(*, provider: str | None = None, model: str | None = None, usage_log: str = DEFAULT_LOG) -> None:
    AitomationApp(provider_override=provider, model_override=model, usage_log=usage_log).run()

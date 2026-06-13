"""CLI entry point. CLI-first for the MLP — a service interface comes later, if ever.

    aitomation discover openapi <spec> [--out inventory.json]

Loads a spec, runs discovery against the configured provider, prints a human-readable
coverage summary, and writes the validated CoverageInventory JSON for downstream stages.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import ConfigError, LLMConfig
from .credentials import (
    DEFAULT_PROFILE,
    PROFILES,
    CredentialError,
    clear_credential,
    clear_profile,
    credential_status,
    get_store,
    needs_credentials,
    profile_fields,
    set_credential,
)
from .diff import InventoryDiff, diff_inventories
from .discover.asyncapi import discover_asyncapi
from .discover.crawl import discover_crawl
from .discover.database import discover_db
from .discover.openapi import discover_openapi
from .discover.registry import discover_registry
from .models import CoverageInventory
from .naming import PROJECTS_ROOT, slugify
from .providers import PydanticAIProvider
from .scaffold import scaffold_project
from .telemetry import DEFAULT_LOG, UsageRecorder, aggregate, load_records
from .workspace import Workspace
from .write import draft_login, draft_tests, enable_drafts, find_skipped_drafts

app = typer.Typer(
    name="aitomation",
    help="Discovery Toolkit: point it at a system, get a coverage inventory.",
    no_args_is_help=True,
    add_completion=False,
)
discover_app = typer.Typer(help="Discover the testable surface of a system.", no_args_is_help=True)
app.add_typer(discover_app, name="discover")
creds_app = typer.Typer(
    help="Store the system-under-test credentials a test run injects (per dev/stage/prod profile).",
    no_args_is_help=True,
)
app.add_typer(creds_app, name="creds")

console = Console()
err = Console(stderr=True)

_PROVIDER_HELP = "Override provider (anthropic/openai/openai-compatible/dashscope)."


@app.command()
def version() -> None:
    """Print the version."""
    console.print(f"aitomation {__version__}")


@app.command()
def tui(
    provider: str | None = typer.Option(None, "--provider", "-p", help=_PROVIDER_HELP),
    model: str | None = typer.Option(None, "--model", "-m", help="Override model name."),
    usage_log: Path = typer.Option(
        Path(DEFAULT_LOG),
        "--usage-log",
        envvar="AITOMATION_USAGE_LOG",
        help="JSONL usage log path.",
    ),
) -> None:
    """Launch the interactive terminal UI (transcript + command prompt + live system sidebar)."""
    from .tui import run as run_tui

    run_tui(provider=provider, model=model, usage_log=str(usage_log))


@app.command()
def usage(
    log: Path = typer.Option(
        Path(DEFAULT_LOG), "--log", envvar="AITOMATION_USAGE_LOG", help="JSONL usage log to read."
    ),
    by: str = typer.Option(
        "app,model",
        "--by",
        help="Comma-separated group keys: app, run_id, model, provider, label, stage.",
    ),
) -> None:
    """Report recorded LLM usage (tokens, latency) grouped by app / model / run / test."""
    records = load_records(log)
    if not records:
        console.print(f"[yellow]No usage recorded[/] at {log}.")
        return

    keys = tuple(k.strip() for k in by.split(",") if k.strip()) or ("app",)
    rows = aggregate(records, keys)

    table = Table(title=f"LLM usage by {', '.join(keys)}")
    for k in keys:
        table.add_column(k, style="cyan", overflow="fold")
    table.add_column("calls", justify="right")
    table.add_column("in", justify="right")
    table.add_column("out", justify="right")
    table.add_column("total", justify="right", style="bold")
    table.add_column("sec", justify="right")
    for g in rows:
        table.add_row(
            *[str(g[k]) for k in keys],
            str(g["calls"]),
            f"{g['input_tokens']:,}",
            f"{g['output_tokens']:,}",
            f"{g['total_tokens']:,}",
            f"{g['duration_s']}",
        )
    console.print(table)

    grand = aggregate(records, ())[0]
    console.print(
        f"[bold]Total:[/] {grand['calls']} calls · "
        f"{grand['input_tokens']:,} in / {grand['output_tokens']:,} out "
        f"({grand['total_tokens']:,} tok) · {grand['duration_s']}s"
    )


def _resolve_provider(
    provider: str | None, model: str | None, recorder: UsageRecorder | None = None
) -> PydanticAIProvider:
    try:
        cfg = LLMConfig.from_env(backend=provider, model=model)
    except ConfigError as e:
        err.print(f"[bold red]Config error:[/] {e}")
        raise typer.Exit(code=2) from None
    console.print(f"[dim]via[/] {cfg.backend}:{cfg.model} [dim](output: {cfg.output_mode})[/]")
    return PydanticAIProvider(cfg, recorder)


def _report_usage(recorder: UsageRecorder) -> None:
    """Flush usage to the log and print a one-line summary (always, even after failure)."""
    if not recorder.records:
        return
    t = recorder.totals
    path = recorder.flush()
    console.print(
        f"[dim]Usage:[/] {t['calls']} call(s) · "
        f"{t['input_tokens']:,} in / {t['output_tokens']:,} out "
        f"({t['total_tokens']:,} tok) · {t['duration_s']}s "
        f"[dim]→ {path} (run {recorder.run_id})[/]"
    )


def _try_load_inventory(path: Path) -> CoverageInventory | None:
    """The inventory already at `path` (the prior discover), or None — used as the diff
    baseline so re-running discover to the same file reports what changed."""
    if not path.exists():
        return None
    try:
        return CoverageInventory.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _print_diff(d: InventoryDiff) -> None:
    def loc(e) -> str:
        return f"{e.method + ' ' if e.method else ''}{e.location}"

    console.print(f"\n[bold]Changes since last inventory[/]  [dim]{d.summary()}[/]")
    for e in d.added_elements:
        console.print(f"  [green]+[/] {e.kind} [bold]{e.name}[/] [dim]{loc(e)}[/]")
    for _old, e in d.changed_elements:
        console.print(f"  [yellow]~[/] {e.kind} [bold]{e.name}[/] [dim]{loc(e)}[/]")
    for e in d.removed_elements:
        console.print(f"  [red]-[/] {e.kind} [bold]{e.name}[/] [dim]{loc(e)}[/]")
    if d.added_journeys:
        console.print(
            f"  [green]+[/] {len(d.added_journeys)} new flow(s): "
            + ", ".join(j.name for j in d.added_journeys)
        )
    if d.affected_journeys:
        console.print(
            "  [yellow]![/] existing flow(s) may be stale (re-draft with "
            "[bold]write --force[/]): " + ", ".join(j.name for j in d.affected_journeys)
        )
    if d.removed_journeys:
        console.print(
            f"  [red]-[/] {len(d.removed_journeys)} flow(s) lost surface (now-removed "
            "elements): " + ", ".join(j.name for j in d.removed_journeys)
        )


def _setup_status(msg: str) -> None:
    # One-off environment-setup notices from a discovery backend (e.g. the first-run
    # Chromium download) — printed so the spinner's silence doesn't read as a hang.
    console.print(f"[yellow]![/] {msg}")


def _run_discovery(coro) -> CoverageInventory:
    """Await a discovery coroutine behind a spinner — a model call can take a minute, and
    silence reads as a hang."""
    with console.status("[bold cyan]discovering[/] — extracting the surface, then one model call…"):
        return asyncio.run(coro)


def _finish(coro, out: Path, *, origin: str | None = None) -> None:
    """Await a discovery coroutine, then write + print the inventory (shared epilogue)."""
    baseline = _try_load_inventory(out)
    try:
        inventory: CoverageInventory = _run_discovery(coro)
    except (FileNotFoundError, ValueError) as e:
        err.print(f"[bold red]Discovery failed:[/] {e}")
        raise typer.Exit(code=1) from None
    except Exception as e:  # network / provider / validation errors
        err.print(f"[bold red]Discovery failed:[/] {type(e).__name__}: {e}")
        raise typer.Exit(code=1) from None

    out.write_text(inventory.model_dump_json(indent=2), encoding="utf-8")
    _print_inventory(inventory)
    if baseline is not None:
        d = diff_inventories(baseline, inventory)
        if not d.is_empty:
            _print_diff(d)
    # Mirror into the shared Workspace (same one the TUI browses) so the system shows up in
    # the library and a later `scaffold`/`write` lands in its run dir. Non-destructive: keeps
    # prior pipeline flags + the run holding any earlier scaffold/drafts.
    if origin is not None:
        Workspace(PROJECTS_ROOT).save(inventory, origin=origin)
    console.print(f"\n[green]✓[/] Inventory written to [bold]{out}[/]")


@discover_app.command("openapi")
def discover_openapi_cmd(
    source: str = typer.Argument(..., help="OpenAPI/Swagger spec: URL or local path."),
    out: Path = typer.Option(
        Path("inventory.json"), "--out", "-o", help="Where to write the inventory JSON."
    ),
    provider: str | None = typer.Option(None, "--provider", "-p", help=_PROVIDER_HELP),
    model: str | None = typer.Option(None, "--model", "-m", help="Override model name."),
    usage_log: Path = typer.Option(
        Path(DEFAULT_LOG),
        "--usage-log",
        envvar="AITOMATION_USAGE_LOG",
        help="JSONL usage log path.",
    ),
) -> None:
    """Discover a CoverageInventory from an OpenAPI/Swagger spec."""
    console.print(f"[dim]Discovering[/] [bold]{source}[/] [dim]…[/]")
    recorder = UsageRecorder(app=source, log_path=usage_log)
    llm = _resolve_provider(provider, model, recorder)
    try:
        _finish(discover_openapi(source, llm), out, origin=source)
    finally:
        _report_usage(recorder)


@discover_app.command("asyncapi")
def discover_asyncapi_cmd(
    source: str = typer.Argument(..., help="AsyncAPI spec (2.x or 3.x): URL or local path."),
    out: Path = typer.Option(
        Path("inventory.json"), "--out", "-o", help="Where to write the inventory JSON."
    ),
    provider: str | None = typer.Option(None, "--provider", "-p", help=_PROVIDER_HELP),
    model: str | None = typer.Option(None, "--model", "-m", help="Override model name."),
    usage_log: Path = typer.Option(
        Path(DEFAULT_LOG),
        "--usage-log",
        envvar="AITOMATION_USAGE_LOG",
        help="JSONL usage log path.",
    ),
) -> None:
    """Discover a CoverageInventory from an AsyncAPI spec (channels → topics, messages → schemas)."""
    console.print(f"[dim]Discovering[/] [bold]{source}[/] [dim]…[/]")
    recorder = UsageRecorder(app=source, log_path=usage_log)
    llm = _resolve_provider(provider, model, recorder)
    try:
        _finish(discover_asyncapi(source, llm), out, origin=source)
    finally:
        _report_usage(recorder)


@discover_app.command("registry")
def discover_registry_cmd(
    source: str = typer.Argument(..., help="Schema registry base URL (Confluent-compatible REST)."),
    out: Path = typer.Option(
        Path("inventory.json"), "--out", "-o", help="Where to write the inventory JSON."
    ),
    provider: str | None = typer.Option(None, "--provider", "-p", help=_PROVIDER_HELP),
    model: str | None = typer.Option(None, "--model", "-m", help="Override model name."),
    usage_log: Path = typer.Option(
        Path(DEFAULT_LOG),
        "--usage-log",
        envvar="AITOMATION_USAGE_LOG",
        help="JSONL usage log path.",
    ),
) -> None:
    """Discover a CoverageInventory from a live schema registry (subjects → event schemas)."""
    console.print(f"[dim]Discovering[/] [bold]{source}[/] [dim]…[/]")
    recorder = UsageRecorder(app=source, log_path=usage_log)
    llm = _resolve_provider(provider, model, recorder)
    try:
        _finish(discover_registry(source, llm), out, origin=source)
    finally:
        _report_usage(recorder)


@discover_app.command("db")
def discover_db_cmd(
    source: str = typer.Argument(
        ..., help="DB connection URL (postgresql://…, sqlite:///…) or a .sql DDL file."
    ),
    out: Path = typer.Option(
        Path("inventory.json"), "--out", "-o", help="Where to write the inventory JSON."
    ),
    provider: str | None = typer.Option(None, "--provider", "-p", help=_PROVIDER_HELP),
    model: str | None = typer.Option(None, "--model", "-m", help="Override model name."),
    usage_log: Path = typer.Option(
        Path(DEFAULT_LOG),
        "--usage-log",
        envvar="AITOMATION_USAGE_LOG",
        help="JSONL usage log path.",
    ),
) -> None:
    """Discover a CoverageInventory from a database (live reflection or a .sql DDL file)."""
    console.print(f"[dim]Discovering[/] [bold]{source}[/] [dim]…[/]")
    recorder = UsageRecorder(app=source, log_path=usage_log)
    llm = _resolve_provider(provider, model, recorder)
    try:
        _finish(discover_db(source, llm), out, origin=source)
    finally:
        _report_usage(recorder)


@discover_app.command("crawl")
def discover_crawl_cmd(
    url: str = typer.Argument(..., help="Base URL of the running web app to crawl."),
    out: Path = typer.Option(
        Path("inventory.json"), "--out", "-o", help="Where to write the inventory JSON."
    ),
    max_pages: int = typer.Option(25, "--max-pages", help="Maximum pages to crawl."),
    max_depth: int = typer.Option(3, "--max-depth", help="Maximum link depth from the start URL."),
    provider: str | None = typer.Option(None, "--provider", "-p", help=_PROVIDER_HELP),
    model: str | None = typer.Option(None, "--model", "-m", help="Override model name."),
    usage_log: Path = typer.Option(
        Path(DEFAULT_LOG),
        "--usage-log",
        envvar="AITOMATION_USAGE_LOG",
        help="JSONL usage log path.",
    ),
) -> None:
    """Discover a CoverageInventory by crawling a running web app (a11y tree, not pixels)."""
    console.print(
        f"[dim]Crawling[/] [bold]{url}[/] [dim](≤{max_pages} pages, depth {max_depth}) …[/]"
    )
    recorder = UsageRecorder(app=url, log_path=usage_log)
    llm = _resolve_provider(provider, model, recorder)
    try:
        _finish(
            discover_crawl(
                url, llm, max_pages=max_pages, max_depth=max_depth, on_status=_setup_status
            ),
            out,
            origin=url,
        )
    finally:
        _report_usage(recorder)


@app.command()
def write(
    inventory_path: Path = typer.Argument(
        ..., exists=True, dir_okay=False, help="Path to a CoverageInventory JSON file."
    ),
    into: Path | None = typer.Option(
        None,
        "--into",
        "-i",
        help="Scaffold directory to write draft tests into. Defaults to projects/<system-name>.",
    ),
    max_journeys: int = typer.Option(8, "--max", help="Max journeys to draft."),
    verify: bool = typer.Option(
        False, "--verify", help="Run drafted tests once and self-heal any failures."
    ),
    force: bool = typer.Option(
        False, "--force", help="Regenerate every flow; default skips flows already drafted."
    ),
    provider: str | None = typer.Option(None, "--provider", "-p", help=_PROVIDER_HELP),
    model: str | None = typer.Option(None, "--model", "-m", help="Override model name."),
    usage_log: Path = typer.Option(
        Path(DEFAULT_LOG),
        "--usage-log",
        envvar="AITOMATION_USAGE_LOG",
        help="JSONL usage log path.",
    ),
) -> None:
    """Draft first-draft pytest+Playwright tests, one per journey, into a scaffold (review-only)."""
    try:
        inv = CoverageInventory.model_validate_json(inventory_path.read_text(encoding="utf-8"))
    except Exception as e:
        err.print(f"[bold red]Invalid inventory:[/] {type(e).__name__}: {e}")
        raise typer.Exit(code=1) from None

    managed = into is None
    if managed:
        # Default → the system's scaffold run dir in the shared Workspace (same layout the TUI
        # uses), so CLI and TUI draft into one set of artifacts. Prefer the latest run (where
        # `scaffold` laid down a runnable framework); create one only if none exists yet.
        ws = Workspace(PROJECTS_ROOT)
        slug = slugify(inv.system_name)
        into = ws.ensure_run(slug)

    if not (into / "conftest.py").exists():
        console.print(
            f"[yellow]![/] {into} doesn't look like a scaffold (no conftest.py). "
            f"Run [bold]aitomation scaffold {inventory_path} -o {into}[/] first for runnable drafts."
        )

    console.print(f"[dim]Drafting tests for[/] [bold]{inv.system_name}[/] [dim]→[/] {into}/tests")
    recorder = UsageRecorder(app=inv.system_name, log_path=usage_log)
    llm = _resolve_provider(provider, model, recorder)
    try:
        report, login = _run_write(
            inv, llm, into=into, max_journeys=max_journeys, verify=verify, force=force
        )
    except Exception as e:
        err.print(f"[bold red]Write failed:[/] {type(e).__name__}: {e}")
        raise typer.Exit(code=1) from None
    finally:
        _report_usage(recorder)

    if managed:
        # Record the draft in the shared index so the TUI library reflects it and re-runs
        # target this same run. Upsert keeps the prior discover's origin/flags intact.
        ws.save(inv)
        ws.set_flags(slug, drafted=True, latest_run=str(into))

    _print_write_report(report, login, verify=verify, into=into)


def _run_write(inv, llm, *, into: Path, max_journeys: int, verify: bool, force: bool):
    """Run draft_tests (+ best-effort login authoring) behind a per-draft progress spinner."""
    with console.status("[bold cyan]drafting[/] …") as status:
        done = 0

        def _tick(res) -> None:
            nonlocal done
            done += 1
            status.update(f"[bold cyan]drafting[/] {done} flow(s) done — last: {res.path.name}")

        report = asyncio.run(
            draft_tests(
                inv,
                llm,
                into=into,
                max_journeys=max_journeys,
                verify=verify,
                force=force,
                on_draft=_tick,
            )
        )
        # Session-auth scaffold → author login.py from the discovered form (no-op otherwise).
        # Best-effort: a failure here never fails the write.
        login = None
        try:
            login = asyncio.run(draft_login(inv, llm, into=into, force=force))
        except Exception as e:
            err.print(f"[yellow]![/] login.py authoring skipped: {type(e).__name__}: {e}")
    return report, login


def _print_write_report(report, login, *, verify: bool, into: Path) -> None:
    """The human summary of a write run — shared by `write` and `go`."""
    if login is not None and login.authored:
        console.print(
            "\n[green]✓[/] Authored [bold]login.py[/] from the discovered sign-in form "
            "[dim](review before trusting; creds come from AUTH_USER/AUTH_PASS).[/]"
        )
    elif login is not None and login.reason and login.reason != "already authored":
        console.print(f"[yellow]![/] login.py kept as the scaffold stub [dim]({login.reason})[/].")

    if report.written:
        n_destructive = sum(1 for r in report.written if r.destructive)
        suffix = f" ({n_destructive} skipped as destructive)" if n_destructive else ""
        console.print(f"\n[green]✓[/] {len(report.written)} draft test(s) written{suffix}:")
        for r in report.written:
            if r.destructive:
                tag = " [yellow](destructive — skipped)[/]"
            elif r.runtime_failed:
                tag = " [red](still failing after --verify — see RUNTIME FAILURE in notes)[/]"
            else:
                tag = ""
            console.print(f"  [dim]·[/] {r.path.name}  [dim](confidence: {r.confidence})[/]{tag}")
        if verify:
            n_failed = sum(1 for r in report.written if r.runtime_failed)
            verdict = f"[red]{n_failed} still failing[/]" if n_failed else "[green]all passing[/]"
            console.print(f"[dim]--verify:[/] ran drafted tests once — {verdict}.")
    if report.quarantined:
        console.print(
            f"\n[yellow]![/] {len(report.quarantined)} draft(s) didn't parse — saved for review:"
        )
        for r in report.quarantined:
            console.print(f"  [dim]·[/] {r.path}")
    if report.skipped:
        console.print(
            f"\n[dim]↻ {len(report.skipped)} flow(s) already drafted — kept "
            f"(use [/][bold]--force[/][dim] to regenerate):[/]"
        )
        for r in report.skipped:
            console.print(f"  [dim]·[/] {r.path.name}")
    if not report.written and not report.quarantined and not report.skipped:
        console.print("[yellow]No journeys to draft.[/]")
        return

    console.print(
        "\n[dim]These are AI first-drafts — review before trusting. "
        f"Run them with[/] [bold]cd {into} && uv run pytest[/]"
    )


def _scaffold_dirs(into: Path) -> list[Path]:
    """Resolve `into` to scaffold dir(s). If `into` itself is a scaffold (has tests/), use it.
    Otherwise treat it as a shared Workspace root and return each system's latest run dir (the
    projects/<slug>/e2e/run-*/ layout the TUI also writes), so `aitomation enable` with the
    default projects/ reaches every generated system — CLI- or TUI-produced alike. Falls back
    to immediate child scaffolds for a plain directory of flat scaffolds."""
    if (into / "tests").is_dir():
        return [into]
    runs = [
        Path(r.latest_run)
        for r in Workspace(into).list_systems()
        if r.latest_run and (Path(r.latest_run) / "tests").is_dir()
    ]
    if runs:
        return runs
    if into.is_dir():
        return [d for d in sorted(into.iterdir()) if (d / "tests").is_dir()]
    return []


@app.command()
def enable(
    tests: list[str] | None = typer.Argument(
        None,
        help="Test(s) to enable, e.g. 'test_create_pet' or 'create_pet.py'. "
        "Omit (and skip --all) to just list the skipped drafts.",
    ),
    into: Path = typer.Option(
        Path(PROJECTS_ROOT),
        "--into",
        "-i",
        help="A scaffold directory, or a parent of scaffolds to scan (default: projects/).",
    ),
    all_: bool = typer.Option(False, "--all", help="Enable EVERY skipped destructive draft found."),
) -> None:
    """Lift the safety skip on destructive draft(s) so they run ('skipped' → 'ok').

    Destructive (mutating) drafts are written with a skip guard so a generated DELETE never
    runs by accident. Review the draft and add teardown FIRST — then enable it here."""
    scaffolds = _scaffold_dirs(into)
    if not scaffolds:
        err.print(
            f"[bold red]No scaffold with a tests/ directory under[/] {into}. "
            f"Point [bold]--into[/] at a scaffold (the one [bold]write[/] reported)."
        )
        raise typer.Exit(code=1)

    # No selection → list mode: show what's skipped so the user can pick.
    if not tests and not all_:
        skipped = [(d, p) for d in scaffolds for p in find_skipped_drafts(d)]
        if not skipped:
            console.print(f"[green]No skipped drafts[/] under {into} — nothing to enable.")
            return
        console.print(f"[bold]{len(skipped)} skipped draft(s):[/]")
        for d, p in skipped:
            console.print(f"  [yellow]·[/] {p.name} [dim]({d})[/]")
        first_dir, first = skipped[0]
        console.print(
            f"\nEnable one with [bold]aitomation enable {first.stem} -i {first_dir}[/], "
            f"or all with [bold]aitomation enable --all -i {into}[/]."
        )
        return

    targets = None if all_ else tests
    results = [r for d in scaffolds for r in enable_drafts(d, targets=targets)]
    enabled = [r for r in results if r.enabled]
    skipped_results = [r for r in results if not r.enabled]

    if enabled:
        console.print(f"\n[green]✓[/] Enabled {len(enabled)} draft(s) — skip guard lifted:")
        for r in enabled:
            console.print(f"  [dim]·[/] {r.path.name}")
        console.print(
            "\n[yellow]![/] These now perform [bold]mutating requests[/] when run. "
            "Confirm teardown is in place before running against a real system."
        )
    # With an explicit target list, surface why a name didn't resolve; under a multi-scaffold
    # scan a 'no such file' per scaffold is just noise, so only show reasons when targeted.
    if targets is not None:
        for r in skipped_results:
            if r.reason != "no such test file" or len(scaffolds) == 1:
                console.print(f"  [dim]·[/] {r.path.name} [dim]({r.reason})[/]")
    if not enabled:
        console.print("[yellow]Nothing enabled.[/]")
        raise typer.Exit(code=1)


@app.command()
def scaffold(
    inventory_path: Path = typer.Argument(
        ..., exists=True, dir_okay=False, help="Path to a CoverageInventory JSON file."
    ),
    out: Path | None = typer.Option(
        None, "--out", "-o", help="Directory to scaffold into. Defaults to projects/<system-name>."
    ),
) -> None:
    """Scaffold a runnable pytest + Playwright project from an inventory (deterministic, no LLM)."""
    try:
        inv = CoverageInventory.model_validate_json(inventory_path.read_text(encoding="utf-8"))
    except Exception as e:
        err.print(f"[bold red]Invalid inventory:[/] {type(e).__name__}: {e}")
        raise typer.Exit(code=1) from None

    managed = out is None
    if managed:
        # Default → a run dir in the shared Workspace (same layout the TUI uses), so CLI and
        # TUI scaffold into one set of artifacts. Reuse the latest run so re-scaffolding
        # refreshes it in place (Copier overwrites framework files, keeps drafted tests/);
        # create one only on first scaffold.
        ws = Workspace(PROJECTS_ROOT)
        slug = slugify(inv.system_name)
        out = ws.ensure_run(slug)

    if out.exists() and any(out.iterdir()):
        console.print(f"[yellow]![/] {out} exists and is non-empty; files may be overwritten.")

    console.print(f"[dim]Scaffolding[/] [bold]{inv.system_name}[/] [dim]→[/] {out} [dim]…[/]")
    try:
        scaffold_project(inv, out, overwrite=True)
    except Exception as e:
        err.print(f"[bold red]Scaffold failed:[/] {type(e).__name__}: {e}")
        raise typer.Exit(code=1) from None

    if managed:
        # Record in the shared index so the TUI library lists this system and `write` targets
        # this run. Upsert keeps any prior discover's origin/flags intact.
        ws.save(inv)
        ws.set_flags(slug, scaffolded=True, latest_run=str(out))

    files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    console.print(f"\n[green]✓[/] Scaffolded {len(files)} files into [bold]{out}[/]:")
    for f in files:
        console.print(f"  [dim]·[/] {f}")
    console.print(
        f"\nNext: [bold]cd {out} && uv sync && uv run playwright install chromium && "
        f"BASE_URL={inv.base_url} uv run pytest[/]"
    )


def _open_store():
    """The active secret store, or exit with an actionable message if none is reachable."""
    try:
        return get_store()
    except CredentialError as e:
        err.print(f"[bold red]Credential store unavailable:[/] {e}")
        raise typer.Exit(code=1) from None


def _resolve_system(system: str) -> tuple[str, CoverageInventory, str]:
    """Find a discovered system by slug or name. Returns (slug, inventory, active_profile)."""
    ws = Workspace(PROJECTS_ROOT)
    target = slugify(system)
    for r in ws.list_systems():
        if r.slug in (system, target):
            return r.slug, ws.load_inventory(r.slug), getattr(r, "profile", DEFAULT_PROFILE)
    err.print(
        f"[bold red]No system[/] matching {system!r} under {PROJECTS_ROOT}. "
        "Run [bold]aitomation discover …[/] first."
    )
    raise typer.Exit(code=1)


@creds_app.command("list")
def creds_list(
    system: str | None = typer.Argument(None, help="Slug or name; omit to list every system."),
    profile: str | None = typer.Option(
        None, "--profile", help="dev/stage/prod (default: the system's active one)."
    ),
) -> None:
    """Show which credentials are stored for a system/profile (values are never shown)."""
    ws = Workspace(PROJECTS_ROOT)
    records = ws.list_systems()
    if system:
        target = slugify(system)
        records = [r for r in records if r.slug in (system, target)]
        if not records:
            err.print(f"[bold red]No system[/] matching {system!r} under {PROJECTS_ROOT}.")
            raise typer.Exit(code=1)
    store = _open_store()
    shown = 0
    for r in records:
        inv = ws.load_inventory(r.slug)
        if not needs_credentials(inv):
            continue
        shown += 1
        prof = profile or getattr(r, "profile", DEFAULT_PROFILE)
        status = credential_status(r.slug, prof, inv, store=store)
        console.print(
            f"\n[bold]{r.name}[/] [dim]({r.slug})[/]  profile: [cyan]{prof}[/]  [dim]· {store.label}[/]"
        )
        for f in profile_fields(inv):
            mark = "[green]●[/]" if status[f.env] else "[yellow]○[/]"
            secret = " [dim](secret)[/]" if f.secret else ""
            console.print(f"  {mark} [bold]{f.env}[/]  {f.label}{secret}")
    if not shown:
        console.print(f"[dim]No systems needing credentials under {PROJECTS_ROOT}.[/]")


@creds_app.command("set")
def creds_set(
    system: str = typer.Argument(..., help="Slug or name of the discovered system."),
    env: str = typer.Argument(
        ..., help="Field to set, e.g. AUTH_TOKEN / AUTH_USER / AUTH_PASS / BASE_URL."
    ),
    profile: str | None = typer.Option(
        None, "--profile", help="dev/stage/prod (default: the system's active one)."
    ),
    value: str | None = typer.Option(
        None, "--value", help="The value (omit to be prompted — hidden for secrets)."
    ),
) -> None:
    """Store one credential for a system/profile (prompted hidden when --value is omitted)."""
    slug, inv, active = _resolve_system(system)
    fields = {f.env: f for f in profile_fields(inv)}
    if env not in fields:
        err.print(f"[bold red]{env}[/] isn't a field for this system. Choose: {', '.join(fields)}.")
        raise typer.Exit(code=1)
    prof = profile or active
    if prof not in PROFILES:
        err.print(f"[bold red]Unknown profile[/] {prof!r}. Choose: {', '.join(PROFILES)}.")
        raise typer.Exit(code=1)
    if value is None:
        import getpass

        value = (
            getpass.getpass(f"{env} ({prof}, hidden): ")
            if fields[env].secret
            else typer.prompt(f"{env} ({prof})")
        )
    store = _open_store()
    set_credential(slug, prof, env, value, store=store)
    console.print(f"[green]✓[/] stored [bold]{env}[/] for {slug} (profile {prof}) in {store.label}")


@creds_app.command("clear")
def creds_clear(
    system: str = typer.Argument(..., help="Slug or name of the discovered system."),
    env: str | None = typer.Argument(
        None, help="Field to clear; omit (or --all) to clear the whole profile."
    ),
    profile: str | None = typer.Option(
        None, "--profile", help="dev/stage/prod (default: the system's active one)."
    ),
    all_: bool = typer.Option(False, "--all", help="Clear every stored value for the profile."),
) -> None:
    """Delete stored credential(s) for a system/profile."""
    slug, inv, active = _resolve_system(system)
    prof = profile or active
    store = _open_store()
    if all_ or env is None:
        n = clear_profile(slug, prof, inv, store=store)
        console.print(f"[green]✓[/] cleared {n} value(s) for {slug} (profile {prof})")
    else:
        clear_credential(slug, prof, env, store=store)
        console.print(f"[green]✓[/] cleared [bold]{env}[/] for {slug} (profile {prof})")


def _print_inventory(inv: CoverageInventory) -> None:
    """Human-readable summary of the inventory to stdout."""
    console.print()
    console.rule(f"[bold]{inv.system_name}")
    console.print(f"[dim]base_url[/]  {inv.base_url}")
    console.print(f"[dim]source[/]    {inv.source}")
    console.print(f"[dim]auth[/]      {inv.auth_strategy or 'none'}")
    if inv.summary:
        console.print(f"\n{inv.summary}")

    kinds = inv.counts_by_kind()
    prios = inv.counts_by_priority()
    console.print(
        f"\n[bold]{len(inv.elements)}[/] testable elements  "
        f"[dim]·[/] by kind: {_fmt_counts(kinds)}  "
        f"[dim]·[/] by priority: {_fmt_counts(prios)}"
    )

    high = [e for e in inv.elements if e.priority == "high"]
    if high:
        table = Table(title="High-priority elements", show_lines=False, expand=False)
        table.add_column("kind", style="cyan", no_wrap=True)
        table.add_column("name")
        table.add_column("location", style="dim")
        for e in high[:25]:
            loc = f"{e.method + ' ' if e.method else ''}{e.location}"
            table.add_row(e.kind, e.name, loc)
        console.print(table)

    if inv.suggested_journeys:
        console.print(f"\n[bold]{len(inv.suggested_journeys)} suggested journeys[/]")
        for j in inv.suggested_journeys:
            console.print(f"  [yellow]●[/] [{j.priority}] [bold]{j.name}[/] — {j.description}")


def _fmt_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "—"
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))


_DB_PREFIXES = ("postgresql://", "postgres://", "mysql://", "sqlite://", "mssql://", "oracle://")


def _sniff_kind(source: str) -> str:
    """Best-effort source-kind detection for `go`. Deterministic and cheap: DB URLs and .sql
    by shape; specs by sniffing the document for its declared standard; an HTML response (or
    an unreachable/odd URL) is treated as a web app to crawl. `--kind` overrides."""
    s = source.lower()
    if s.endswith(".sql") or s.startswith(_DB_PREFIXES):
        return "db"
    text: str | None = None
    if s.startswith(("http://", "https://")):
        try:
            import httpx

            resp = httpx.get(source, timeout=10, follow_redirects=True)
            if "html" in resp.headers.get("content-type", ""):
                return "crawl"
            text = resp.text
        except Exception:
            return "crawl"
    else:
        p = Path(source)
        if p.exists():
            text = p.read_text(encoding="utf-8", errors="ignore")
    head = (text or "")[:4000]
    if '"asyncapi"' in head or "asyncapi:" in head:
        return "asyncapi"
    if any(tok in head for tok in ('"openapi"', "openapi:", '"swagger"', "swagger:")):
        return "openapi"
    return "crawl" if s.startswith(("http://", "https://")) else "openapi"


@app.command()
def go(
    source: str = typer.Argument(
        ...,
        help="What to point at: an OpenAPI/AsyncAPI spec (URL or file), a running web app URL, "
        "a DB connection URL / .sql file, or a schema registry URL (with --kind registry).",
    ),
    kind: str = typer.Option(
        "auto",
        "--kind",
        "-k",
        help="Source kind: auto | openapi | asyncapi | crawl | db | registry.",
    ),
    max_journeys: int = typer.Option(8, "--max", help="Max journeys to draft."),
    verify: bool = typer.Option(
        False, "--verify", help="Run drafted tests once and self-heal any failures."
    ),
    max_pages: int = typer.Option(25, "--max-pages", help="Crawl bound: maximum pages."),
    max_depth: int = typer.Option(3, "--max-depth", help="Crawl bound: maximum link depth."),
    provider: str | None = typer.Option(None, "--provider", "-p", help=_PROVIDER_HELP),
    model: str | None = typer.Option(None, "--model", "-m", help="Override model name."),
    usage_log: Path = typer.Option(
        Path(DEFAULT_LOG),
        "--usage-log",
        envvar="AITOMATION_USAGE_LOG",
        help="JSONL usage log path.",
    ),
) -> None:
    """The whole pipeline in one command: discover → scaffold → draft tests."""
    k = kind.lower() if kind != "auto" else _sniff_kind(source)
    if k not in ("openapi", "asyncapi", "crawl", "db", "registry"):
        err.print(f"[bold red]Unknown --kind[/] {kind!r} (openapi/asyncapi/crawl/db/registry).")
        raise typer.Exit(code=2)
    auto_note = " [dim](auto-detected — override with --kind)[/]" if kind == "auto" else ""
    console.print(f"[dim]Source kind:[/] [bold]{k}[/]{auto_note}")

    recorder = UsageRecorder(app=source, log_path=usage_log)
    llm = _resolve_provider(provider, model, recorder)
    if k == "crawl":
        coro = discover_crawl(
            source, llm, max_pages=max_pages, max_depth=max_depth, on_status=_setup_status
        )
    else:
        discoverer = {
            "openapi": discover_openapi,
            "asyncapi": discover_asyncapi,
            "db": discover_db,
            "registry": discover_registry,
        }[k]
        coro = discoverer(source, llm)

    try:
        inv = _run_discovery(coro)
    except Exception as e:
        _report_usage(recorder)
        err.print(f"[bold red]Discovery failed:[/] {type(e).__name__}: {e}")
        raise typer.Exit(code=1) from None

    _print_inventory(inv)
    ws = Workspace(PROJECTS_ROOT)
    slug = slugify(inv.system_name)
    ws.save(inv, origin=source)
    run = ws.ensure_run(slug)
    console.print(f"\n[green]✓[/] Discovered [dim](inventory in {PROJECTS_ROOT}/{slug}/.aito/)[/]")

    try:
        scaffold_project(inv, run, overwrite=True)
    except Exception as e:
        _report_usage(recorder)
        err.print(f"[bold red]Scaffold failed:[/] {type(e).__name__}: {e}")
        raise typer.Exit(code=1) from None
    ws.set_flags(slug, scaffolded=True, latest_run=str(run))
    console.print(f"[green]✓[/] Scaffolded [dim](deterministic, no LLM)[/] → [bold]{run}[/]")

    try:
        report, login = _run_write(
            inv, llm, into=run, max_journeys=max_journeys, verify=verify, force=False
        )
    except Exception as e:
        err.print(f"[bold red]Write failed:[/] {type(e).__name__}: {e}")
        raise typer.Exit(code=1) from None
    finally:
        _report_usage(recorder)
    ws.set_flags(slug, drafted=True)
    _print_write_report(report, login, verify=verify, into=run)
    console.print(
        f"\nNext: [bold]cd {run} && uv sync && uv run playwright install chromium && uv run pytest[/]"
    )


@app.command()
def schema() -> None:
    """Print the CoverageInventory JSON Schema — the versioned contract of inventory files."""
    import json

    typer.echo(json.dumps(CoverageInventory.model_json_schema(), indent=2))


# Help lists commands in WORKFLOW order, not file-definition order: the first thing a new
# user reads should read as the pipeline. Sub-typers (discover, creds) always list after.
_HELP_ORDER = ["tui", "go", "scaffold", "write", "enable", "schema", "usage", "version"]
app.registered_commands.sort(
    key=lambda c: _HELP_ORDER.index(c.name or c.callback.__name__)  # type: ignore[union-attr]
)


def main() -> None:
    # Load .env so BYO-key works out of the box. Real env vars win (override=False);
    # done only at the CLI boundary so the library stays free of implicit env loading.
    from dotenv import load_dotenv

    load_dotenv()
    app()


if __name__ == "__main__":
    main()

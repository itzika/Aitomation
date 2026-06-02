"""CLI entry point. CLI-first for the MLP — a service interface comes later, if ever.

    aitomation discover openapi <spec> [--out inventory.json]

Loads a spec, runs discovery against the configured provider, prints a human-readable
coverage summary, and writes the validated CoverageInventory JSON for downstream stages.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import ConfigError, LLMConfig
from .diff import InventoryDiff, diff_inventories
from .discover.crawl import discover_crawl
from .discover.openapi import discover_openapi
from .models import CoverageInventory
from .naming import PROJECTS_ROOT, slugify
from .providers import PydanticAIProvider
from .scaffold import scaffold_project
from .telemetry import DEFAULT_LOG, UsageRecorder, aggregate, load_records
from .write import draft_tests, enable_drafts, find_skipped_drafts

app = typer.Typer(
    name="aitomation",
    help="Discovery Toolkit: point it at a system, get a coverage inventory.",
    no_args_is_help=True,
    add_completion=False,
)
discover_app = typer.Typer(help="Discover the testable surface of a system.", no_args_is_help=True)
app.add_typer(discover_app, name="discover")

console = Console()
err = Console(stderr=True)

_PROVIDER_HELP = "Override provider (anthropic/openai/openai-compatible/dashscope)."


@app.command()
def version() -> None:
    """Print the version."""
    console.print(f"aitomation {__version__}")


@app.command()
def tui(
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help=_PROVIDER_HELP),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override model name."),
    usage_log: Path = typer.Option(
        Path(DEFAULT_LOG), "--usage-log", envvar="AITOMATION_USAGE_LOG", help="JSONL usage log path."
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
    provider: Optional[str], model: Optional[str], recorder: UsageRecorder | None = None
) -> PydanticAIProvider:
    try:
        cfg = LLMConfig.from_env(backend=provider, model=model)
    except ConfigError as e:
        err.print(f"[bold red]Config error:[/] {e}")
        raise typer.Exit(code=2)
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
    except Exception:  # noqa: BLE001 — not a valid prior inventory → no baseline
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


def _finish(coro, out: Path) -> None:
    """Await a discovery coroutine, then write + print the inventory (shared epilogue)."""
    baseline = _try_load_inventory(out)
    try:
        inventory: CoverageInventory = asyncio.run(coro)
    except (FileNotFoundError, ValueError) as e:
        err.print(f"[bold red]Discovery failed:[/] {e}")
        raise typer.Exit(code=1)
    except Exception as e:  # network / provider / validation errors
        err.print(f"[bold red]Discovery failed:[/] {type(e).__name__}: {e}")
        raise typer.Exit(code=1)

    out.write_text(inventory.model_dump_json(indent=2), encoding="utf-8")
    _print_inventory(inventory)
    if baseline is not None:
        d = diff_inventories(baseline, inventory)
        if not d.is_empty:
            _print_diff(d)
    console.print(f"\n[green]✓[/] Inventory written to [bold]{out}[/]")


@discover_app.command("openapi")
def discover_openapi_cmd(
    source: str = typer.Argument(..., help="OpenAPI/Swagger spec: URL or local path."),
    out: Path = typer.Option(
        Path("inventory.json"), "--out", "-o", help="Where to write the inventory JSON."
    ),
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help=_PROVIDER_HELP),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override model name."),
    usage_log: Path = typer.Option(
        Path(DEFAULT_LOG), "--usage-log", envvar="AITOMATION_USAGE_LOG", help="JSONL usage log path."
    ),
) -> None:
    """Discover a CoverageInventory from an OpenAPI/Swagger spec."""
    console.print(f"[dim]Discovering[/] [bold]{source}[/] [dim]…[/]")
    recorder = UsageRecorder(app=source, log_path=usage_log)
    llm = _resolve_provider(provider, model, recorder)
    try:
        _finish(discover_openapi(source, llm), out)
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
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help=_PROVIDER_HELP),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override model name."),
    usage_log: Path = typer.Option(
        Path(DEFAULT_LOG), "--usage-log", envvar="AITOMATION_USAGE_LOG", help="JSONL usage log path."
    ),
) -> None:
    """Discover a CoverageInventory by crawling a running web app (a11y tree, not pixels)."""
    console.print(f"[dim]Crawling[/] [bold]{url}[/] [dim](≤{max_pages} pages, depth {max_depth}) …[/]")
    recorder = UsageRecorder(app=url, log_path=usage_log)
    llm = _resolve_provider(provider, model, recorder)
    try:
        _finish(discover_crawl(url, llm, max_pages=max_pages, max_depth=max_depth), out)
    finally:
        _report_usage(recorder)


@app.command()
def write(
    inventory_path: Path = typer.Argument(
        ..., exists=True, dir_okay=False, help="Path to a CoverageInventory JSON file."
    ),
    into: Optional[Path] = typer.Option(
        None,
        "--into",
        "-i",
        help="Scaffold directory to write draft tests into. "
        "Defaults to projects/<system-name>.",
    ),
    max_journeys: int = typer.Option(8, "--max", help="Max journeys to draft."),
    verify: bool = typer.Option(False, "--verify", help="Run drafted tests once and self-heal any failures."),
    force: bool = typer.Option(
        False, "--force", help="Regenerate every flow; default skips flows already drafted."
    ),
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help=_PROVIDER_HELP),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override model name."),
    usage_log: Path = typer.Option(
        Path(DEFAULT_LOG), "--usage-log", envvar="AITOMATION_USAGE_LOG", help="JSONL usage log path."
    ),
) -> None:
    """Draft first-draft pytest+Playwright tests, one per journey, into a scaffold (review-only)."""
    try:
        inv = CoverageInventory.model_validate_json(inventory_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        err.print(f"[bold red]Invalid inventory:[/] {type(e).__name__}: {e}")
        raise typer.Exit(code=1)

    if into is None:
        into = Path(PROJECTS_ROOT) / slugify(inv.system_name)

    if not (into / "conftest.py").exists():
        console.print(
            f"[yellow]![/] {into} doesn't look like a scaffold (no conftest.py). "
            f"Run [bold]aitomation scaffold {inventory_path} -o {into}[/] first for runnable drafts."
        )

    console.print(f"[dim]Drafting tests for[/] [bold]{inv.system_name}[/] [dim]→[/] {into}/tests")
    recorder = UsageRecorder(app=inv.system_name, log_path=usage_log)
    llm = _resolve_provider(provider, model, recorder)
    try:
        report = asyncio.run(
            draft_tests(inv, llm, into=into, max_journeys=max_journeys, verify=verify, force=force)
        )
    except Exception as e:  # noqa: BLE001
        err.print(f"[bold red]Write failed:[/] {type(e).__name__}: {e}")
        raise typer.Exit(code=1)
    finally:
        _report_usage(recorder)

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
            verdict = (
                f"[red]{n_failed} still failing[/]" if n_failed else "[green]all passing[/]"
            )
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
    """Resolve `into` to scaffold dir(s). If `into` itself is a scaffold (has tests/), use it;
    otherwise treat it as a container and return its immediate children that are scaffolds — so
    `aitomation enable` with the default projects/ scans every generated system."""
    if (into / "tests").is_dir():
        return [into]
    if into.is_dir():
        return [d for d in sorted(into.iterdir()) if (d / "tests").is_dir()]
    return []


@app.command()
def enable(
    tests: Optional[list[str]] = typer.Argument(
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
    all_: bool = typer.Option(
        False, "--all", help="Enable EVERY skipped destructive draft found."
    ),
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
    out: Optional[Path] = typer.Option(
        None, "--out", "-o", help="Directory to scaffold into. Defaults to projects/<system-name>."
    ),
) -> None:
    """Scaffold a runnable pytest + Playwright project from an inventory (deterministic, no LLM)."""
    try:
        inv = CoverageInventory.model_validate_json(inventory_path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        err.print(f"[bold red]Invalid inventory:[/] {type(e).__name__}: {e}")
        raise typer.Exit(code=1)

    if out is None:
        out = Path(PROJECTS_ROOT) / slugify(inv.system_name)

    if out.exists() and any(out.iterdir()):
        console.print(f"[yellow]![/] {out} exists and is non-empty; files may be overwritten.")

    console.print(f"[dim]Scaffolding[/] [bold]{inv.system_name}[/] [dim]→[/] {out} [dim]…[/]")
    try:
        scaffold_project(inv, out, overwrite=True)
    except Exception as e:  # noqa: BLE001
        err.print(f"[bold red]Scaffold failed:[/] {type(e).__name__}: {e}")
        raise typer.Exit(code=1)

    files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    console.print(f"\n[green]✓[/] Scaffolded {len(files)} files into [bold]{out}[/]:")
    for f in files:
        console.print(f"  [dim]·[/] {f}")
    console.print(
        f"\nNext: [bold]cd {out} && uv sync && uv run playwright install chromium && "
        f"BASE_URL={inv.base_url} uv run pytest[/]"
    )


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


def main() -> None:
    # Load .env so BYO-key works out of the box. Real env vars win (override=False);
    # done only at the CLI boundary so the library stays free of implicit env loading.
    from dotenv import load_dotenv

    load_dotenv()
    app()


if __name__ == "__main__":
    main()

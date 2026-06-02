# Aitomation — Discovery Toolkit

Point it at a system → get a structured **coverage inventory**, a runnable
Playwright + pytest scaffold, and **first-draft tests per journey**. Model-agnostic,
BYO-key, self-hostable.

The wedge is **Discover**: understand the system *first*. Most AI test writers hallucinate
because they have no system model. The `CoverageInventory` is that model.

> Determinism boundary: this tool **discovers, scaffolds, and drafts**. It never decides
> pass/fail. AI is the analyst and author, never the judge. Scaffolding is pure templating
> (no LLM); committed tests use deterministic Playwright assertions.

## Status

| Stage | What | State |
|-------|------|-------|
| **Discover** | OpenAPI/Swagger → validated `CoverageInventory` | ✅ working |
| **Discover** | Live crawl of a running web app (a11y tree) → inventory | ✅ working |
| **Scaffold** | Copier → runnable pytest + pytest-playwright project | ✅ working |
| **Write** | First-draft test per journey, into the scaffold (review-only) | ✅ working |

> The crawler uses Playwright (Python) directly for a bounded, deterministic BFS — not an
> agent driving the Playwright MCP — so artifacts are reproducible and testable.

## Setup

```bash
uv sync
```

## Interactive TUI (Workbench)

A master-detail terminal app (neon-on-dark theme): a browsable **Systems** library on the
left, a tabbed **System view** (Overview · Coverage · Flows · Tests · Usage) on the right, a
**live log**, an onboarding **wizard**, and a command palette.

```bash
uv run aitomation tui
```

- **Browsable library** — every discovered system is listed with pipeline progress dots
  (discover · scaffold · write); persisted and re-openable across sessions.
- **Onboarding** — press `n` for a guided wizard (source → location → model).
- **Drill-down** — Coverage (testable elements with inputs & auth preconditions), Flows
  (suggested end-to-end paths), Tests (drafts with source preview), Usage (token cost).
- **Overview** shows token **cost**: discover total, and tests drafted with avg tokens/test
  and suite total.
- **Run from the TUI** — `t` runs the scaffolded tests (streams pytest to the live log;
  **pytest** decides pass/fail, never the AI) and `o` opens the run folder in your editor.
  The Overview also lists the exact `uv` / `docker` commands.
- **Keys**: `n` new · `s` scaffold · `w` write · `r` re-discover · `t` run · `o` open ·
  `d` delete · `l` toggle log · `?` help · `q` quit · `Ctrl+P` palette.

Generated artifacts are written to a visible, timestamped run per generation:
`<output>/<app-slug>/e2e/run-<YYYYMMDD-HHMMSS>/` (default `<output>` = `projects/`). Each run
is a self-contained, independently runnable scaffold + drafts.

## Configure a provider (BYO-key)

Model-agnostic. Pick a backend via env; nothing is hardcoded.

| Backend | Env | Notes |
|---------|-----|-------|
| `anthropic` | `ANTHROPIC_API_KEY` | Claude (default) |
| `openai` | `OPENAI_API_KEY` | OpenAI models |
| `dashscope` | `DASHSCOPE_API_KEY` | Alibaba Qwen (OpenAI-compatible); free tokens, good for prototyping |
| `openai-compatible` | `AITOMATION_API_KEY` + `AITOMATION_BASE_URL` | Any local/OpenAI-compatible server (Ollama, vLLM) |

```bash
export AITOMATION_PROVIDER=dashscope
export DASHSCOPE_API_KEY=sk-...
# optional overrides: AITOMATION_MODEL, AITOMATION_BASE_URL, AITOMATION_TEMPERATURE
```

Local model example (Ollama):

```bash
export AITOMATION_PROVIDER=openai-compatible
export AITOMATION_BASE_URL=http://localhost:11434/v1
export AITOMATION_MODEL=qwen2.5-coder:7b
```

> Capability scales with the model — local 7B models discover worse than frontier models.

## Discover from an OpenAPI spec

```bash
# the Rick and Morty API (real public API; spec reconstructed from the live service)
uv run aitomation discover openapi examples/rickandmorty.openapi.json -m qwen3-max

# minimal petstore demo
uv run aitomation discover openapi examples/petstore-mini.json --out inventory.json

# also accepts a URL:
uv run aitomation discover openapi https://example.com/openapi.json
# override provider/model per-run:
uv run aitomation discover openapi spec.yaml -p dashscope -m qwen3-max
```

> Rick and Morty publishes no OpenAPI document, so `examples/rickandmorty.openapi.json`
> was reconstructed from its live responses.

You get a human-readable summary on stdout and the validated `CoverageInventory` JSON at
`--out` for downstream stages.

## Discover by crawling a running app

No spec needed — point it at a live URL. The crawler does a bounded same-origin BFS,
snapshotting the accessibility tree (not pixels), forms, and links.

```bash
uv run aitomation discover crawl https://rickandmortyapi.com/ --max-pages 8 -m qwen3-max
# bounds: --max-pages (default 25), --max-depth (default 3)
```

First time only, install the browser: `uv run playwright install chromium`.

## Scaffold a runnable test project

Deterministic templating from an inventory (Copier; **no LLM**). Adapts to what was
discovered — page objects for web pages/forms, an API client for endpoints, and an auth
fixture matched to the discovered scheme: bearer token, **API key in its actual header**
(e.g. `api_key`), HTTP basic, or a session/login flow.

```bash
uv run aitomation scaffold inventory.json                # → projects/<system-name>/
cd projects/<system-name> && uv sync && uv run playwright install chromium && uv run pytest
```

By default each system is scaffolded into `projects/<system-name>/`; pass `-o <dir>` to
override.

Produces `conftest.py` (fixtures), `pages.py` (Page Objects with seeded role/label
locators + `fill()`) / `api_client.py`, a runnable smoke test, `pyproject.toml` (uv) with
pro defaults (**trace + screenshot retained on failure**, registered markers), `.env.example`,
`Dockerfile`, a GitHub Actions workflow, and a reporting-hook *interface*. Session auth uses
the **`storage_state` pattern** — log in once, reuse across tests.

## Write first-draft tests per journey

Drafts one runnable pytest+Playwright test per suggested journey, **into** a scaffold. The
AI authors deterministic assertions once; the test runner judges them — AI never decides
pass/fail. Drafts land as files for review and are **never auto-merged**.

```bash
uv run aitomation scaffold inventory.json                 # 1. skeleton → projects/<system-name>/
uv run aitomation write inventory.json -m qwen3-max       # 2. draft journey tests (same dir)
cd projects/<system-name> && uv run pytest                # 3. run them
```

`scaffold` and `write` default to the same `projects/<system-name>/` directory, so they line
up automatically; pass `-o`/`-i` to point elsewhere.

Each draft is **lint-enforced**, not just hoped: web flows must drive a Page Object and use
Playwright's web-first `expect()`; API flows must use the request fixture; hard sleeps are
banned. A non-conforming draft is regenerated once with the findings fed back, and if it
still doesn't conform it's **quarantined** to `drafts_needs_review/` (out of the runnable
suite) with the reasons in its header. Every file carries a provenance header.

> They are *first drafts*: expect to fix selectors and tighten assertions. In a live run
> against the Rick & Morty API, 6 of 7 drafts passed as-generated; the 7th encoded a too-
> strict assumption about a filter's semantics — exactly what review is for.

## Enable a skipped (destructive) draft

Drafts for **mutating** journeys (create/update/delete, or password-bearing forms) are
written with a `pytest.mark.skip` guard so a generated `DELETE` never runs against a real
system by accident — they're emitted but skipped. After you've reviewed one and added
teardown, lift the guard to turn it from *skipped* → *ok* (deterministic, no LLM):

```bash
uv run aitomation enable                          # list skipped drafts under projects/
uv run aitomation enable test_create_pet          # enable one (in its scaffold)
uv run aitomation enable --all -i projects/store   # enable all in a given scaffold
```

`enable` only removes the guard *this tool* injected; a skip you added by hand is left alone.

## Measure LLM usage

Every model call is instrumented — prompt, model, tokens in/out, latency — keyed by run
and by app, appended to a JSONL log (`usage.jsonl` by default; `--usage-log` to override).
This is how you compare models/providers on cost and quality (the model-agnostic thesis).

```bash
aitomation usage --by app,model     # cost per system, per model
aitomation usage --by label         # per prompt / per test (e.g. write:test_pet_lifecycle)
aitomation usage --by run_id        # per run
```

Discovery calls are labelled by stage (`discover.openapi`, `discover.crawl`); each Write
draft is labelled by the test it produced, so token cost is attributable per test.

## How discovery works

1. **Deterministic extraction** — the spec is parsed (or the site crawled) into a factual
   surface. For OpenAPI, **every endpoint becomes a `TestableElement`** with its inputs and
   auth preconditions enumerated deterministically — the model cannot invent or drop
   endpoints, so coverage is complete and stable run-to-run.
2. **LLM judgement only** — the model ranks priorities, infers `auth_strategy`, and proposes
   end-to-end journeys over the *fixed* element list → a validated `CoverageInventory`. It
   decides what matters, never what exists.
3. **Backfill** — ground-truth fields (base URL, system name, source) are set
   deterministically, never trusted to the model.

## Develop

```bash
uv run pytest        # no API key needed: the LLM seam is stubbed; browser tests skip if no Chromium
```

Layout: `src/aitomation/{models,config,providers}.py`, `src/aitomation/discover/{openapi,crawl}.py`,
`src/aitomation/scaffold/` (generator + Copier template), `src/aitomation/write/` (journey
test drafting), CLI in `src/aitomation/cli.py`.

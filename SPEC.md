# SPEC.md — Discovery Toolkit Architecture

> Reconciled 2026-06: v1 (the MLP and more) has SHIPPED. This now records the architecture
> as built, and defines v2 scope so the scope guardrails in CLAUDE.md stay meaningful.

## Product in one line

Point it at a system → get a structured coverage inventory, a runnable Playwright+pytest
framework, and first-draft tests. Model-agnostic, BYO-key, self-hostable.

## The four stages

```
DISCOVER  →  SCAFFOLD  →  WRITE  →  DEPLOY
(AI core)    (templating)  (AI)     (plumbing)
 shipped      shipped      shipped   later
```

### 1. Discover — the differentiated core (shipped)

Inputs (five backends, each a subcommand and a wizard source):
- An **OpenAPI / Swagger** spec (URL or file)
- An **AsyncAPI** spec (channels → topics, messages → schemas)
- A **schema registry** (Confluent-compatible REST)
- A **database** (live reflection or a `.sql` DDL file)
- A **running web app** — a bounded, deterministic same-origin BFS crawl using Playwright
  (Python) directly over the accessibility tree. NOT an agent driving a browser, and not
  the Playwright MCP (early drafts said MCP; the deterministic crawl won for
  reproducibility and testability).

Process — the three-layer discovery design, the architectural core:
1. **Deterministic extraction**: the spec is parsed (or the site crawled) into a factual
   surface. Every endpoint/page/form/topic/table becomes a `TestableElement`
   deterministically — the model cannot invent or drop surface.
2. **LLM judgement only**: one structured call ranks priorities, infers `auth_strategy`,
   and proposes journeys over the *fixed* element list. It decides what matters, never
   what exists.
3. **Deterministic backfill**: ground-truth fields (base URL, names, source, auth schemes,
   `schema_version`) are set by code, never trusted to the model.

Output: a validated `CoverageInventory` (Pydantic; see `models.py` — the file carries a
`schema_version`, and `aitomation schema` prints the JSON Schema contract). This is the
system model everything downstream consumes, and the artifact that accumulates value:
`diff.py` compares inventories across discovers so re-discovery is incremental.

### 2. Scaffold — deterministic templating, NOT AI (shipped)

Copier + post-copy rendering emit a **professional framework layout**:

```
projects/<slug>/e2e/run-<stamp>/
├── conftest.py            # fixtures: base_url + auth matched to the discovered scheme
├── pages/                 # one module per page object over a shared BasePage;
│   ├── base_page.py       #   __init__.py re-exports so `from pages import X` holds
│   └── <page>.py
├── api/                   # ApiClient seeded from discovered endpoints
├── support/               # reporting hook interface (triage is a separate product)
├── tests/                 # test_smoke.py; drafts route to web/ api/ contract/
├── pyproject.toml         # uv-managed; pythonpath, markers, trace-on-failure defaults
├── Dockerfile, .github/workflows/e2e.yml, .env.example
└── login.py               # session auth only; authored by Write from the real form
```

Auth fixture kinds: bearer, API key in its *actual* header/query, HTTP basic, or a
session/login flow on the `storage_state` pattern. No LLM anywhere in this stage.

### 3. Write — AI-assisted, human-authoritative (shipped)

One draft per journey, grounded on only the elements that journey touches. Drafts are
generated concurrently (bounded), lint-gated (page-object use, web-first `expect`, no hard
sleeps, valid assertion methods), regenerated once on findings, quarantined to
`drafts_needs_review/` if still non-conforming. Mutating journeys are skip-guarded
deterministically (`aitomation enable` lifts the guard after review). `--verify` runs each
draft once and self-heals failures (the TUI's `f`). Never auto-merged.

### 4. Deploy — later

CI wiring, parallelization, results hook. Still out of scope (the scaffold ships a CI
workflow stub; that's the line).

## LLM provider abstraction (decided: Pydantic AI)

`LLMProvider` protocol with `generate` / `generate_structured`; one Pydantic AI-backed
implementation normalizes Anthropic, OpenAI, DashScope (Qwen), and any OpenAI-compatible
local server. Structured-output mode is configurable (tool / prompted / native) because
local servers reject large tool-call payloads. Prompt caching is on for Anthropic; system
prompts are single-variant so caches actually hit. Every call is recorded (tokens, cache,
latency, per-stage model) — the Usage tab/command makes the model-agnostic thesis measurable.

## Front-ends

CLI-first (`discover`/`scaffold`/`write`/`enable`/`creds`/`usage`/`schema`, plus
`aitomation go <source>` running the whole pipeline) and the Workbench TUI — both drive the
same pipeline and share the `projects/<slug>/e2e/run-*` workspace.

## v2 scope (the next wedge, in priority order)

1. **Authenticated crawling** — wire the existing creds store + authored `perform_login`
   into the crawler (scripted, reviewable login → `storage_state` → crawl the authed
   surface). Most real systems sit behind a login; this is the demo→product gap.
2. **Large-spec chunking** — chunk the *judgement* call (extraction is already unbounded)
   per resource group with a merge pass, so 500-endpoint enterprise specs work.
3. **GraphQL introspection + Postman collections** — two deterministic discovery backends.
4. **`diff` as a CI command** — discover `--against baseline`, exit codes + markdown
   report; nightly drift detection in the generated workflow. The inventory as a living
   artifact (this was deliberately deferred from v1).
5. **`coverage` report card** — deterministic mapping of elements ↔ journeys ↔ drafts ↔
   last run: "X% of high-priority surface has a passing test".
6. **Static HTML report** (`aitomation report`) — a self-contained shareable artifact.
   Static file yes; hosted dashboard NO (that's the deferred triage product).
7. **MCP server mode** — expose discover/scaffold/write/diff as MCP tools.
8. **Discovery eval harness** — golden inventories + deterministic scoring × recorded
   cost: "which model discovers best per dollar", reproducible.
9. **PyPI release** — `uvx aitomation`; trusted publishing workflow.

## Explicitly OUT of scope (unchanged)

Dashboard, RBAC, SaaS/multi-tenant, results DB, the triage/reporting-intelligence layer
(separate product), visual regression, an agentic "AI explores your app" crawler (a
bounded-interaction crawl may be researched in v3; determinism stays the differentiator).
No further investment in backend surfaces (events/DB) until the web+API wedge converts.

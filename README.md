# Aitomation

Point it at a system — it maps what's testable, scaffolds a Playwright + pytest project, and writes first-draft tests. You review and run. AI never decides pass/fail.

Works with OpenAPI specs, AsyncAPI, databases, and live web apps. Bring your own API key. Model-agnostic.

<!-- ![Aitomation Workbench](docs/img/overview.png) -->

<p align="center">
  <img src="docs/img/overview.png" alt="The Aitomation Workbench — systems library, system Overview, live log" width="900">
</p>

---

## Install

Requires [uv](https://github.com/astral-sh/uv).

```bash
git clone https://github.com/itzika/Aitomation
cd Aitomation
uv sync
uv run playwright install chromium
```

Set a provider key (pick one):

```bash
export ANTHROPIC_API_KEY=sk-...          # Claude (default)
export OPENAI_API_KEY=sk-...             # OpenAI
export DASHSCOPE_API_KEY=sk-...          # Alibaba Qwen (free tokens, good for prototyping)
```

Or use a local model via Ollama:

```bash
export AITOMATION_PROVIDER=openai-compatible
export AITOMATION_BASE_URL=http://localhost:11434/v1
export AITOMATION_MODEL=qwen2.5-coder:7b
```

A `.env` file in the repo root is loaded automatically.

---

## Use

### Option A — TUI (recommended)

```bash
uv run aitomation tui
```

Press `n` to add a new system via a guided wizard. Then:

| Key | Action |
|-----|--------|
| `n` | New system (wizard) |
| `s` | Scaffold a pytest + Playwright project |
| `w` | Write first-draft tests |
| `t` | Run tests |
| `f` | Fix failing tests (one retry) |
| `m` | Switch model / provider |
| `?` | Help |

### Option B — CLI

```bash
# 1. Discover your system
uv run aitomation discover openapi https://example.com/openapi.json --out inventory.json

# 2. Scaffold a test project
uv run aitomation scaffold inventory.json

# 3. Write draft tests
uv run aitomation write inventory.json

# 4. Run them
cd projects/<system-name> && uv run pytest
```

Other discovery sources:

```bash
uv run aitomation discover asyncapi  examples/asyncapi.yaml
uv run aitomation discover db        postgresql://user@host/db
uv run aitomation discover crawl     https://example.com --max-pages 8
```

---

## How it works

1. **Discover** — parses your spec or crawls your app into a `CoverageInventory`: every testable endpoint, form, and schema, enumerated deterministically. The model can't invent or drop items.
2. **Scaffold** — generates a runnable pytest + Playwright project from the inventory. No LLM involved — pure templating.
3. **Write** — drafts one test per user journey. Drafts are written to a `drafts/` folder for your review. Mutating tests (create/delete/etc.) are skipped by default until you explicitly enable them.

Artifacts land at: `projects/<app-slug>/e2e/run-<YYYYMMDD-HHMMSS>/`

---

## What's supported

| Source | Status |
|--------|--------|
| OpenAPI / Swagger | ✅ |
| AsyncAPI | ✅ |
| Schema registry (Confluent-compatible) | ✅ |
| Database (live or `.sql` DDL) | ✅ |
| Live web crawl | ✅ |

| Provider | Key env var |
|----------|-------------|
| Anthropic (Claude) | `ANTHROPIC_API_KEY` |
| OpenAI | `OPENAI_API_KEY` |
| Alibaba Qwen (DashScope) | `DASHSCOPE_API_KEY` |
| Any OpenAI-compatible (Ollama, vLLM) | `AITOMATION_API_KEY` + `AITOMATION_BASE_URL` |

---

## Develop

```bash
uv run pytest   # LLM seam is stubbed; browser tests skip if no Chromium
```

MIT License.

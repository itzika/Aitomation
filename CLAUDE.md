# CLAUDE.md — Discovery Toolkit

> Context artifact for Claude Code. Read this and SPEC.md before doing anything.
> This is the primary handoff document from the ideation session into implementation.

## What this is

A model-agnostic toolkit that compresses the cold-start of test automation. Point it at
a system (running app or spec) and it produces:

1. A **structured coverage inventory** of what's testable (the differentiated core).
2. A **scaffolded Playwright + pytest framework** ready to run.
3. **First-draft tests** for the mapped journeys (review-only, never auto-merged).

The wedge is **Discover** — understand the system first. Incumbents (QA Wolf, mabl,
Tricentis/Testim, Applitools) lead with authoring and treat discovery as an afterthought.
That gap is the differentiation.

## Who's building this

Senior test automation engineer pivoting to AI engineering. Solo, bootstrapped. Strong
in: Python, FastAPI, pytest, Playwright, Postgres/pgvector, Docker, Kafka, React/TS.
Heavy Claude Code user. Uses `uv` for all Python project management (NOT pip+venv).

## Hard constraints (do not violate)

- **Determinism boundary.** The tool DISCOVERS, SCAFFOLDS, and DRAFTS. It NEVER decides
  pass/fail. Committed tests use deterministic Playwright assertions. AI is the analyst
  and author, never the judge. This is a selling point for enterprise trust, not a limit.
- **Human-authoritative output.** Generated tests and scaffolds land as files/PRs for
  review. Nothing auto-merges or auto-commits.
- **Model-agnostic.** Must work with Anthropic, OpenAI, and OpenAI-compatible local
  servers (Qwen/Ollama/vLLM) as well as alibaba qwen models (https://modelstudio.console.alibabacloud.com/ap-southeast-1 , lots of free tokens, great for prototyping during the system development) . Use a provider abstraction — evaluate LiteLLM vs Pydantic
  AI during scaffold; lean Pydantic AI for typed structured output. Capability scales with
  model; design agnostic but set expectations that local 7B models discover worse.
- **BYO-key / self-hostable.** Enterprise QA data is sensitive. No mandatory managed
  inference. VPC/self-host story matters. Design for it from day one, don't retrofit.
- **uv for everything.** Project init, deps, Python version, venv. Never pip+venv.

## Scope discipline (READ THIS — known failure mode)

The builder has a documented pattern of meta-tool scope creep. This idea is the most
creep-prone in the space. Guardrails:

- MLP = **Discover + Scaffold only.** Write is a thin follow-on. Deploy is later.
- Do NOT build: a dashboard, RBAC, multi-tenant SaaS plumbing, a results database, or a
  triage layer (that's a SEPARATE product, deliberately deferred) in the MLP.
- If a feature isn't on the critical path from "point at system" → "runnable skeleton +
  draft tests", it is out of scope for v1. Flag scope creep proactively.

## Stack

- Python 3.12+, managed with `uv`
- Playwright (Python) + the official Playwright MCP for the crawl
- Pydantic for all structured data models (coverage inventory is the central schema)
- FastAPI only if/when a service interface is needed — CLI-first for the MLP
- Copier for the scaffold templates
- pytest + pytest-playwright as the generated framework target

## Working style for this engineer

- Direct, minimal output. No filler.
- Always give architecture options with tradeoffs, then a recommendation.
- Proactively flag bad ideas and scope creep — don't just agree.
- CLAUDE.md is the primary context artifact across all projects.

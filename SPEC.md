# SPEC.md — Discovery Toolkit Architecture

## Product in one line

Point it at a system → get a structured coverage inventory, a runnable Playwright+pytest
framework, and first-draft tests. Model-agnostic, BYO-key, self-hostable.

## The four stages

```
DISCOVER  →  SCAFFOLD  →  WRITE  →  DEPLOY
(AI core)    (templating)  (AI)     (plumbing)
 ^^^ MLP focus ^^^         follow-on  later
```

### 1. Discover  — the differentiated core

Input (one or more):
- A running app URL (crawl via Playwright MCP using the accessibility tree, not pixels)
- An OpenAPI / Swagger spec
- A Postman collection

Process:
- Crawl/parse the surface. For web: walk routes, detect forms, auth flows, key journeys,
  interactive elements via the a11y tree. For APIs: enumerate endpoints, methods, schemas.
- Feed the raw crawl artifacts to the LLM (via the provider abstraction) to produce a
  **structured, validated coverage inventory** — NOT tests yet.

Output: `CoverageInventory` (Pydantic model). This is the central artifact everything
else consumes. Rough shape:

```python
class TestableElement(BaseModel):
    kind: Literal["page", "form", "flow", "endpoint", "auth"]
    name: str
    location: str            # URL or endpoint path
    description: str
    inputs: list[Input]      # fields, params, body schema
    preconditions: list[str] # e.g. "requires authenticated session"
    priority: Literal["high", "medium", "low"]

class CoverageInventory(BaseModel):
    system_name: str
    base_url: str
    auth_strategy: str | None     # "oauth2" | "session" | "basic" | None
    elements: list[TestableElement]
    suggested_journeys: list[Journey]
```

Why this is the moat: most AI test writers hallucinate because they have no system model.
The inventory IS the system model. It also accumulates value — over time you can diff
inventories across versions to detect surface changes (a future hook, not MLP).

### 2. Scaffold — deterministic templating (NOT AI)

From the inventory, generate a framework skeleton with Copier:
- pytest + pytest-playwright structure
- fixtures (incl. an auth fixture chosen from `inventory.auth_strategy`)
- page-object structure seeded from discovered pages
- config, `pyproject.toml` (uv-managed), Docker, CI workflow stub
- a reporting hook (left as an interface — triage is a separate product)

AI's only role here: pick sensible defaults from the inventory. The generation itself is
deterministic templating so output is reproducible.

### 3. Write — AI-assisted, human-authoritative (thin follow-on)

For each high-priority journey/element in the inventory, generate a first-draft pytest-
playwright test into the scaffold. Lands as files for review. Never auto-merged.
The inventory context is what makes these good rather than generic.

### 4. Deploy — later

CI wiring, parallelization, results hook. Out of MLP scope. Noted for completeness.

## LLM provider abstraction

Single internal interface:

```python
class LLMProvider(Protocol):
    async def generate(self, prompt: str, *, system: str | None = None) -> str: ...
    async def generate_structured[T](self, prompt: str, schema: type[T]) -> T: ...
```

- `generate_structured` is the critical one — discovery depends on reliable JSON matching
  a Pydantic schema. Providers differ on JSON/tool-calling; the abstraction normalizes
  this and validates with Pydantic regardless of backend.
- Evaluate **Pydantic AI** (typed, structured-first, agnostic) vs **LiteLLM** (broadest
  provider coverage incl. local) during scaffold. Lean Pydantic AI.
- Adapters: Anthropic, OpenAI, OpenAI-compatible (covers Qwen/Ollama/vLLM in one).
- Config-driven provider+model selection. BYO-key via env/config, never hardcoded.

## MLP definition (build this, nothing more)

A CLI that:
1. Takes a URL or an OpenAPI spec.
2. Produces a validated `CoverageInventory` (JSON + human-readable summary).
3. Scaffolds a runnable pytest+playwright project from it via Copier.
4. Generates 5–10 first-draft tests for high-priority journeys.

Demo target: point it at a sample app, get back a skeleton you can `uv run pytest` on,
with draft tests, in minutes instead of days. That demo sells itself.

## Explicitly OUT of scope for v1

Dashboard, RBAC, SaaS/multi-tenant, results DB, the triage/reporting-intelligence layer
(separate product), inventory-diffing, visual regression. Resist all of it.

## Suggested build order

1. Provider abstraction + one adapter (Anthropic), prove `generate_structured`.
2. OpenAPI-spec discovery path first (deterministic input, easiest to validate the
   inventory schema) BEFORE the live-crawl path.
3. Live-crawl discovery via Playwright MCP.
4. Scaffold via Copier from the inventory.
5. Write draft tests.
6. Add OpenAI + OpenAI-compatible adapters once the pipeline works end-to-end.

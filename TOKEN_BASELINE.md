# Token Baseline & Results

> Post-fix results are at the bottom. Baseline (pre-optimization) follows.

## Post-fix results (measured)

Identical work re-run on Anthropic (`claude-sonnet-4-6`) after the three fixes, isolated to
`/tmp/post_fix_usage.jsonl`: `discover openapi` on `examples/rickandmorty.openapi.json` +
`write --max 6` (12 write calls incl. corrective retries).

**Fix #1 — Anthropic caching now ON.** Per-call `cache_read_tokens` for the write run:
```
[0, 1847, 1847, 1847, 1847, 1847, 1847, 1847, 1847, 1847, 1847, 1847]
 ^ first call writes the cache; every subsequent call reads the 1847-token prefix at 0.1x
```
Baseline Anthropic `cache_read` was **0** across all 105 calls.

Cost of the cacheable prefix (system prompt + tool schema = 1847 tok) across a 12-call run,
Sonnet pricing (in $3 / cache-write $3.75 / cache-read $0.30 per MTok):
- baseline (no cache): 12 × 1847 × $3 = **$0.0665**
- post-fix: 1×1847×$3.75 + 11×1847×$0.30 = **$0.0130** → **~80% reduction on the prefix**, and
  the saving grows with calls-per-run (more journeys = more cache hits amortizing one write).

**Fix #2 — one system prompt variant.** Write system-prompt length is now a single value
(`5418` chars) across all 12 calls, vs **4 variants** (2797/3110/3476/3797) at baseline. The
prompt is intentionally larger (carries all rule sets) but is cached, so the per-call cost
drops anyway. One stable prefix also helps DashScope/OpenAI implicit caching.

**Fix #3 — exceptions-only priorities.** Discover output schema no longer echoes a
`{name, priority}` per element; the model returns only `high_priority`/`low_priority` name
lists. Verified end-to-end: the R&M inventory came back `{high: 3, low: 3, medium: 0}` with
no medium elements echoed. All 6 drafts + smoke test collect cleanly (`7 tests collected`).

---

# Token Baseline — pre-optimization

Snapshot of `usage.jsonl` **before** the three token optimizations, so we can measure the
effect afterward. Captured from 272 historical calls.

## Per-(stage, provider, model) averages

| label | provider | model | calls | avgIn | avgOut | avgCacheRead | avgSysChars |
|---|---|---|---|---|---|---|---|
| discover.crawl | dashscope | qwen-plus | 18 | 5223 | 2196 | 85 | 917 |
| discover.crawl | anthropic | claude-sonnet-4-6 | 5 | 4806 | 3523 | **0** | 870 |
| discover.crawl | anthropic | claude-opus-4-8 | 3 | 1797 | 1990 | **0** | 751 |
| fix | dashscope | qwen-plus | 27 | 3933 | 649 | 1460 | 3797 |
| fix | anthropic | claude-sonnet-4-5 | 7 | 4552 | 589 | **0** | 3797 |
| fix | anthropic | claude-sonnet-4-6 | 6 | 3524 | 689 | **0** | 3797 |
| fix | anthropic | claude-opus-4-8 | 4 | 8025 | 914 | **0** | 3797 |
| write | dashscope | qwen-plus | 122 | 1361 | 581 | 777 | 3437 |
| write | anthropic | claude-sonnet-4-6 | 62 | 2042 | 1407 | **0** | 3357 |
| write | anthropic | claude-opus-4-8 | 16 | 2285 | 966 | **0** | 3476 |

## Totals
- input: **644,036**
- output: **266,280**
- cache_read: 135,872 (all DashScope/OpenAI implicit; Anthropic = 0)

## What each fix targets
1. **Anthropic caching** — Anthropic: 105 calls, cache_read **0** → confirms caching is OFF.
   Target: system prompt + tool schema billed at 0.1× after the first call.
2. **Static write system prompt** — write currently has **4 variants**, which fragment the
   cache prefix:
   - 2797 chars × 18 calls
   - 3110 chars × 52 calls
   - 3476 chars × 70 calls
   - 3797 chars × 60 calls

   Target: collapse to 1 variant so the cached prefix is reused across all write/fix calls.
3. **Exceptions-only priorities** — `InventoryJudgment` echoes a `{name, priority}` per
   element. Target: emit only HIGH/LOW names → cuts `discover.*` output tokens.

## Re-measurement protocol (apples-to-apples)
After implementing, run the *same* operations against a known example and compare per-call
averages for the same provider/model. Suggested fixed inputs:

```bash
# tag the cutoff so we only aggregate NEW records
wc -l usage.jsonl   # note the line count = BASELINE_LINES

# re-run identical work (example: anthropic backend)
AITOMATION_PROVIDER=anthropic uv run aitomation discover examples/rickandmorty.openapi.json -o /tmp/inv.json
AITOMATION_PROVIDER=anthropic uv run aitomation scaffold /tmp/inv.json --into /tmp/rm
AITOMATION_PROVIDER=anthropic uv run aitomation write /tmp/inv.json --into /tmp/rm

# then aggregate only the new tail of usage.jsonl (rows after BASELINE_LINES) and compare:
#   - anthropic cache_read should jump from 0 to ~80-90% of the cached prefix
#   - write should show a SINGLE system-prompt length
#   - discover.* avgOut should drop
```

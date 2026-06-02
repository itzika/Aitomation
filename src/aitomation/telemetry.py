"""LLM usage instrumentation.

Captures every model call — prompt, model, tokens in/out, latency — keyed by run and by
app (system under test), so cost/latency/quality can be compared across models and
providers later. This directly serves the model-agnostic thesis: it's how you quantify
"local 7B discovers worse (and cheaper?)" instead of asserting it.

Records are appended as JSONL (one line per call) so later reporting is trivial: load,
group by app/run/model/label, aggregate. The `aitomation usage` command does exactly that.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DEFAULT_LOG = os.environ.get("AITOMATION_USAGE_LOG", "usage.jsonl")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class CallRecord:
    """One LLM call. `label` identifies the prompt/operation (e.g. 'discover.openapi',
    'write:petLifecycle'); `app` is the system under test."""

    run_id: str
    app: str
    label: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_read_tokens: int
    requests: int
    duration_s: float
    started_at: str
    ended_at: str
    ok: bool
    error: str | None
    system_prompt: str
    user_prompt: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class UsageRecorder:
    """Collects CallRecords for a single CLI run, then appends them to a JSONL log.

    One recorder per run (it owns the run_id and the app under test). The provider writes
    to it after every call; the CLI flushes it at the end (even on failure)."""

    def __init__(
        self,
        *,
        app: str,
        run_id: str | None = None,
        log_path: str | Path = DEFAULT_LOG,
        capture_prompts: bool = True,
    ) -> None:
        self.app = app
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.log_path = Path(log_path)
        self.capture_prompts = capture_prompts
        self.records: list[CallRecord] = []
        self._flushed = 0  # index into records already written to disk

    def record(
        self,
        *,
        label: str,
        provider: str,
        model: str,
        system: str | None,
        user: str,
        usage: Any | None,
        duration_s: float,
        started_at: str,
        ended_at: str,
        ok: bool = True,
        error: str | None = None,
    ) -> CallRecord:
        # getattr-with-default so this tolerates providers that report partial/no usage.
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        rec = CallRecord(
            run_id=self.run_id,
            app=self.app,
            label=label,
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cache_read_tokens=int(getattr(usage, "cache_read_tokens", 0) or 0),
            requests=int(getattr(usage, "requests", 0) or 0),
            duration_s=round(duration_s, 3),
            started_at=started_at,
            ended_at=ended_at,
            ok=ok,
            error=error,
            system_prompt=(system or "") if self.capture_prompts else "",
            user_prompt=user if self.capture_prompts else "",
        )
        self.records.append(rec)
        return rec

    def flush(self) -> Path:
        """Append not-yet-written records to the JSONL log. Safe to call repeatedly across a
        long session — only new records are appended each time."""
        pending = self.records[self._flushed:]
        if not pending:
            return self.log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            for r in pending:
                f.write(json.dumps(r.to_dict()) + "\n")
        self._flushed = len(self.records)
        return self.log_path

    @property
    def totals(self) -> dict[str, Any]:
        return {
            "calls": len(self.records),
            "input_tokens": sum(r.input_tokens for r in self.records),
            "output_tokens": sum(r.output_tokens for r in self.records),
            "total_tokens": sum(r.total_tokens for r in self.records),
            "duration_s": round(sum(r.duration_s for r in self.records), 3),
        }


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------


def load_records(log_path: str | Path) -> list[dict[str, Any]]:
    path = Path(log_path)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def aggregate(
    records: Iterable[dict[str, Any]], by: tuple[str, ...]
) -> list[dict[str, Any]]:
    """Group records by the given keys and sum token/latency metrics."""
    groups: dict[tuple, dict[str, Any]] = {}
    for r in records:
        key = tuple(str(r.get(k, "")) for k in by)
        g = groups.setdefault(
            key,
            {**{k: r.get(k, "") for k in by}, "calls": 0,
             "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "duration_s": 0.0},
        )
        g["calls"] += 1
        g["input_tokens"] += int(r.get("input_tokens", 0))
        g["output_tokens"] += int(r.get("output_tokens", 0))
        g["total_tokens"] += int(r.get("total_tokens", 0))
        g["duration_s"] += float(r.get("duration_s", 0.0))
    rows = list(groups.values())
    rows.sort(key=lambda g: g["total_tokens"], reverse=True)
    for g in rows:
        g["duration_s"] = round(g["duration_s"], 3)
    return rows

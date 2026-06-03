"""Tests for LLM usage instrumentation: recording, persistence, and aggregation."""

from __future__ import annotations

from dataclasses import dataclass

from aitomation.telemetry import UsageRecorder, aggregate, load_records


@dataclass
class _FakeUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    requests: int = 1


def _rec(**kw):
    """Helper to record one call with sensible defaults."""
    defaults = {
        "label": "discover.openapi",
        "provider": "dashscope",
        "model": "qwen3-max",
        "system": "sys",
        "user": "prompt body",
        "usage": _FakeUsage(input_tokens=100, output_tokens=40),
        "duration_s": 1.5,
        "started_at": "t0",
        "ended_at": "t1",
    }
    defaults.update(kw)
    return defaults


def test_record_captures_tokens_and_prompt():
    r = UsageRecorder(app="Demo", run_id="run1")
    rec = r.record(**_rec())
    assert rec.input_tokens == 100 and rec.output_tokens == 40
    assert rec.total_tokens == 140
    assert rec.run_id == "run1" and rec.app == "Demo"
    assert rec.user_prompt == "prompt body" and rec.system_prompt == "sys"
    assert r.totals == {
        "calls": 1,
        "input_tokens": 100,
        "output_tokens": 40,
        "total_tokens": 140,
        "duration_s": 1.5,
    }


def test_record_tolerates_missing_usage():
    r = UsageRecorder(app="Demo")
    rec = r.record(**_rec(usage=None))
    assert rec.input_tokens == 0 and rec.output_tokens == 0 and rec.total_tokens == 0


def test_capture_prompts_false_omits_bodies():
    r = UsageRecorder(app="Demo", capture_prompts=False)
    rec = r.record(**_rec())
    assert rec.user_prompt == "" and rec.system_prompt == ""
    # but token counts are still captured
    assert rec.total_tokens == 140


def test_flush_writes_jsonl_and_reloads(tmp_path):
    log = tmp_path / "usage.jsonl"
    r = UsageRecorder(app="Demo", run_id="run1", log_path=log)
    r.record(**_rec(label="discover.openapi"))
    r.record(**_rec(label="write:test_a", usage=_FakeUsage(input_tokens=10, output_tokens=5)))
    path = r.flush()
    assert path == log

    records = load_records(log)
    assert len(records) == 2
    assert {rec["label"] for rec in records} == {"discover.openapi", "write:test_a"}

    # appends rather than overwrites
    UsageRecorder(app="Demo2", run_id="run2", log_path=log).record(**_rec()) and None
    r2 = UsageRecorder(app="Demo2", run_id="run2", log_path=log)
    r2.record(**_rec())
    r2.flush()
    assert len(load_records(log)) == 3


def test_flush_noop_when_empty(tmp_path):
    log = tmp_path / "usage.jsonl"
    UsageRecorder(app="Demo", log_path=log).flush()
    assert not log.exists()


def test_aggregate_groups_and_sums():
    records = [
        {
            "app": "A",
            "model": "m1",
            "label": "discover.openapi",
            "input_tokens": 100,
            "output_tokens": 40,
            "total_tokens": 140,
            "duration_s": 1.0,
        },
        {
            "app": "A",
            "model": "m1",
            "label": "write:test_x",
            "input_tokens": 50,
            "output_tokens": 20,
            "total_tokens": 70,
            "duration_s": 0.5,
        },
        {
            "app": "B",
            "model": "m2",
            "label": "discover.openapi",
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "duration_s": 0.2,
        },
    ]
    by_app_model = aggregate(records, ("app", "model"))
    assert len(by_app_model) == 2
    top = by_app_model[0]  # sorted by total_tokens desc
    assert top["app"] == "A" and top["calls"] == 2 and top["total_tokens"] == 210

    # per-test (label) breakdown — answers "tokens per test"
    by_label = aggregate(records, ("label",))
    write_row = next(r for r in by_label if r["label"] == "write:test_x")
    assert write_row["total_tokens"] == 70

    grand = aggregate(records, ())[0]
    assert grand["calls"] == 3 and grand["total_tokens"] == 225

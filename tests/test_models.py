"""Model-level guards for the backend-surface extensions (kinds/sources/where + json_schema).

Pure Pydantic validation — no LLM. Confirms the new literals are accepted, the optional
json_schema field round-trips, and the inventory's count helpers see the new kinds.

`TestableElement` is referenced via the `models` module rather than imported by name:
a module-scope name starting with "Test" makes pytest try (and warn) to collect it.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aitomation import models
from aitomation.models import CoverageInventory, InputField


def test_new_element_kinds_and_where_accepted():
    table = models.TestableElement(
        kind="table",
        name="orders",
        location="public.orders",
        description="Orders table.",
        inputs=[
            InputField(name="id", type="integer", required=True, where="column"),
            InputField(name="email", type="text", required=True, where="column"),
        ],
        preconditions=["PRIMARY KEY (id)", "UNIQUE (email)"],
        priority="high",
    )
    assert table.kind == "table"
    assert [i.where for i in table.inputs] == ["column", "column"]


def test_event_schema_carries_json_schema():
    schema = {"type": "object", "required": ["orderId"], "properties": {"orderId": {"type": "string"}}}
    el = models.TestableElement(
        kind="event_schema",
        name="OrderCreated",
        location="orders.created",
        method="publish",
        description="Emitted when an order is created.",
        inputs=[InputField(name="orderId", type="string", required=True, where="message")],
        json_schema=schema,
        priority="high",
    )
    # round-trips through serialization (the scaffold reads this back out)
    reparsed = models.TestableElement.model_validate_json(el.model_dump_json())
    assert reparsed.json_schema == schema
    assert reparsed.inputs[0].where == "message"


def test_json_schema_defaults_none_for_other_kinds():
    el = models.TestableElement(
        kind="endpoint", name="getPet", location="/pets/{id}", method="GET",
        description="Read a pet.", priority="medium",
    )
    assert el.json_schema is None


def test_counts_by_kind_includes_backend_kinds():
    inv = CoverageInventory(
        system_name="Mixed", base_url="x", source="db_schema",
        elements=[
            models.TestableElement(kind="table", name="t1", location="t1", description="", priority="low"),
            models.TestableElement(kind="table", name="t2", location="t2", description="", priority="low"),
            models.TestableElement(kind="topic", name="evt", location="evt", description="", priority="high"),
        ],
    )
    assert inv.counts_by_kind() == {"table": 2, "topic": 1}


def test_unknown_kind_still_rejected():
    with pytest.raises(ValidationError):
        models.TestableElement(kind="queue", name="x", location="x", description="", priority="low")

"""Tests for database discovery — both ingestion modes.

DDL mode parses the example .sql via sqlglot. Live mode reflects an in-memory SQLite
database (no external driver needed), so the SQLAlchemy Inspector branch is exercised for
real. The judgement call is stubbed; we test the deterministic extraction and the seam.
"""

from __future__ import annotations

from pathlib import Path
from typing import TypeVar

import pytest
from pydantic import BaseModel
from sqlalchemy import create_engine, text

from aitomation.discover.database import (
    discover_db,
    elements_from_db_summary,
    parse_ddl,
    reflect_database,
    summarize_ddl_file,
)
from aitomation.models import CoverageInventory

DDL = Path(__file__).parent.parent / "examples" / "schema.sql"

T = TypeVar("T", bound=BaseModel)


# --------------------------------------------------------------------------------------
# DDL mode (sqlglot)
# --------------------------------------------------------------------------------------


def test_parse_ddl_columns_and_constraints():
    tables = {t.name: t for t in parse_ddl(DDL.read_text())}
    assert set(tables) == {"users", "orders", "order_items"}

    users = tables["users"]
    by_col = {c.name: c for c in users.columns}
    assert by_col["id"].primary_key is True and by_col["id"].required is True
    assert by_col["email"].required is True  # NOT NULL
    assert by_col["full_name"].required is False
    assert any("UNIQUE (email)" in c for c in users.constraints)

    orders = tables["orders"]
    assert any("FOREIGN KEY" in c and "users" in c for c in orders.constraints)

    # composite, table-level PK marks both columns required + pk
    items = tables["order_items"]
    pk_cols = {c.name for c in items.columns if c.primary_key}
    assert pk_cols == {"order_id", "sku"}
    assert all(c.required for c in items.columns if c.name in pk_cols)


def test_ddl_summary_and_elements():
    summary = summarize_ddl_file(str(DDL))
    assert summary.source_kind == "ddl" and summary.base_url == str(DDL)
    elements = elements_from_db_summary(summary)
    tables = {e.name: e for e in elements if e.kind == "table"}
    assert set(tables) == {"users", "orders", "order_items"}
    email = next(i for i in tables["users"].inputs if i.name == "email")
    assert email.where == "column" and email.required is True


def test_summarize_ddl_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        summarize_ddl_file("/nope/missing.sql")


def test_parse_ddl_ignores_non_table_statements():
    tables = parse_ddl("CREATE INDEX ix ON t (a); CREATE TABLE t (a INT NOT NULL);")
    assert [t.name for t in tables] == ["t"]


# --------------------------------------------------------------------------------------
# Live mode (SQLAlchemy reflection against in-memory SQLite)
# --------------------------------------------------------------------------------------


def _seed_sqlite(path: str) -> None:
    e = create_engine(path)
    with e.begin() as c:
        c.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE)"))
        c.execute(
            text(
                "CREATE TABLE orders (id INTEGER PRIMARY KEY, "
                "user_id INTEGER NOT NULL REFERENCES users(id), total NUMERIC NOT NULL)"
            )
        )
    e.dispose()


def test_reflect_live_sqlite(tmp_path):
    db = f"sqlite:///{tmp_path / 'app.db'}"
    _seed_sqlite(db)
    summary = reflect_database(db)
    assert summary.source_kind == "live" and summary.dialect == "sqlite"
    tables = {t.name: t for t in summary.tables}
    assert set(tables) == {"users", "orders"}

    users = {c.name: c for c in tables["users"].columns}
    # SQLite reports INTEGER PRIMARY KEY as nullable; PK membership forces required.
    assert users["id"].primary_key is True and users["id"].required is True
    assert users["email"].required is True
    assert any("UNIQUE" in c for c in tables["users"].constraints)
    assert any("FOREIGN KEY" in c and "users" in c for c in tables["orders"].constraints)


def test_reflect_bad_url_raises_clean_error():
    with pytest.raises(ValueError):
        reflect_database("not-a-url")


class _FakeJudge:
    def __init__(self) -> None:
        self.last_system: str | None = None

    async def generate(self, prompt, *, system=None, label=""):  # pragma: no cover
        return ""

    async def generate_structured(self, prompt, schema: type[T], *, system=None, label: str = "") -> T:
        self.last_system = system
        return schema(
            system_summary="An e-commerce schema.",
            auth_strategy="should-be-ignored",
            high_priority=["orders"],
            low_priority=[],
            suggested_journeys=[],
        )


async def test_discover_db_ddl_end_to_end():
    provider = _FakeJudge()
    inv = await discover_db(str(DDL), provider)
    assert isinstance(inv, CoverageInventory)
    assert inv.source == "db_schema"
    assert inv.auth_strategy is None  # forced null for a schema, ignoring the model
    assert inv.counts_by_kind().get("table") == 3
    assert next(e for e in inv.elements if e.name == "orders").priority == "high"
    assert provider.last_system and "database schema" in provider.last_system.lower()


async def test_discover_db_rejects_ambiguous_source():
    with pytest.raises(ValueError):
        await discover_db("just-some-string", _FakeJudge())

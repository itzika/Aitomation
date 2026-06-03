"""Database discovery path — two ingestion modes behind one entry point.

A database has a first-class contract too: its schema. We model each table as a `table`
element (columns become `column` inputs, constraints become preconditions) so the scaffold
can draft *schema/constraint* contract tests — deterministic, read-only assertions against
the catalog. As with the other paths, the LLM only supplies judgement.

Two modes, chosen from the source:
- a **connection URL** (`postgresql://`, `mysql://`, `sqlite:///...`) → live reflection via
  SQLAlchemy's dialect-portable `Inspector`. The most accurate, and the in-VPC/self-host
  deployment the spec targets.
- a **`.sql` DDL file** → parsed with `sqlglot` (dialect-aware `CREATE TABLE` AST), for when
  there's no live database to point at.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..models import CoverageInventory, InputField, TestableElement
from ..providers import LLMProvider
from .asyncapi import _unique
from .openapi import InventoryJudgment, render_elements_for_prompt

MAX_TABLES = 500
MAX_COLUMNS = 100


# --------------------------------------------------------------------------------------
# Shapes
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class ColumnInfo:
    name: str
    type: str
    required: bool  # NOT NULL, or part of the primary key
    primary_key: bool = False


@dataclass(slots=True)
class TableInfo:
    name: str
    columns: list[ColumnInfo] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)  # human-readable PK/FK/UNIQUE/CHECK
    schema: str | None = None


@dataclass(slots=True)
class DBSummary:
    system_name: str
    base_url: str  # connection string with password redacted, or the DDL file path
    dialect: str
    source_kind: str  # "live" | "ddl"
    tables: list[TableInfo] = field(default_factory=list)
    truncated: bool = False


def _is_connection_url(source: str) -> bool:
    return "://" in source and not source.startswith(("http://", "https://"))


# --------------------------------------------------------------------------------------
# Live reflection (SQLAlchemy Inspector)
# --------------------------------------------------------------------------------------


def reflect_database(url: str) -> DBSummary:
    """Reflect a live database via SQLAlchemy. Raises a clean ValueError (not a raw driver
    traceback) when the DBAPI driver is missing or the database can't be reached."""
    try:
        from sqlalchemy import create_engine, inspect
        from sqlalchemy.engine import make_url
    except ImportError as e:  # pragma: no cover - sqlalchemy is a hard dep
        raise ValueError(f"SQLAlchemy is required for live DB discovery: {e}") from e

    try:
        url_obj = make_url(url)
    except Exception as e:
        raise ValueError(f"Invalid database URL {url!r}: {e}") from e

    backend = url_obj.get_backend_name()
    redacted = url_obj.render_as_string(hide_password=True)
    db_name = url_obj.database or backend

    try:
        engine = create_engine(url)
    except (ImportError, ModuleNotFoundError) as e:
        raise ValueError(
            f"No database driver for dialect {url_obj.drivername!r}. Install one — e.g. "
            f"`uv add psycopg` (PostgreSQL) or `uv add pymysql` (MySQL). ({e})"
        ) from e
    except Exception as e:
        raise ValueError(f"Could not create engine for {redacted}: {type(e).__name__}: {e}") from e

    summary = DBSummary(
        system_name=f"{db_name} ({backend} schema)",
        base_url=redacted,
        dialect=backend,
        source_kind="live",
    )
    try:
        insp = inspect(engine)
        names = insp.get_table_names()
        summary.truncated = len(names) > MAX_TABLES
        for tname in names[:MAX_TABLES]:
            summary.tables.append(_reflect_table(insp, tname))
    except (ImportError, ModuleNotFoundError) as e:
        raise ValueError(
            f"No database driver for dialect {url_obj.drivername!r}; install it. ({e})"
        ) from e
    except Exception as e:
        raise ValueError(f"Could not introspect {redacted}: {type(e).__name__}: {e}") from e
    finally:
        engine.dispose()
    return summary


def _reflect_table(insp, tname: str) -> TableInfo:
    pk = insp.get_pk_constraint(tname) or {}
    pk_cols = list(pk.get("constrained_columns") or [])
    pk_set = set(pk_cols)

    columns = [
        ColumnInfo(
            name=col["name"],
            type=str(col["type"]),
            # SQLite reports INTEGER PRIMARY KEY as nullable; PK membership forces required.
            required=(not col.get("nullable", True)) or col["name"] in pk_set,
            primary_key=col["name"] in pk_set,
        )
        for col in insp.get_columns(tname)[:MAX_COLUMNS]
    ]

    constraints: list[str] = []
    if pk_cols:
        constraints.append(f"PRIMARY KEY ({', '.join(pk_cols)})")
    for fk in insp.get_foreign_keys(tname):
        cc = ", ".join(fk.get("constrained_columns") or [])
        rc = ", ".join(fk.get("referred_columns") or [])
        constraints.append(f"FOREIGN KEY ({cc}) -> {fk.get('referred_table')}({rc})")
    for u in insp.get_unique_constraints(tname):
        constraints.append(f"UNIQUE ({', '.join(u.get('column_names') or [])})")
    try:
        for c in insp.get_check_constraints(tname):
            constraints.append(f"CHECK {c.get('sqltext', '')}".strip())
    except Exception:
        pass
    return TableInfo(name=tname, columns=columns, constraints=constraints)


# --------------------------------------------------------------------------------------
# DDL file parsing (sqlglot)
# --------------------------------------------------------------------------------------


def parse_ddl(text: str, *, dialect: str | None = None) -> list[TableInfo]:
    """Parse `CREATE TABLE` statements out of a DDL script into TableInfo. Best-effort and
    dialect-tolerant: unrecognised statements are skipped, not fatal."""
    import sqlglot
    from sqlglot import exp

    try:
        statements = sqlglot.parse(text, read=dialect)
    except Exception as e:  # sqlglot.errors.ParseError and friends
        raise ValueError(f"Could not parse DDL: {type(e).__name__}: {e}") from e

    tables: list[TableInfo] = []
    for stmt in statements:
        if not isinstance(stmt, exp.Create) or (stmt.kind or "").upper() != "TABLE":
            continue
        schema_node = stmt.this
        if not isinstance(schema_node, exp.Schema):
            continue  # e.g. CREATE TABLE ... AS SELECT — no column defs to read
        table_node = schema_node.this
        tname = getattr(table_node, "name", None)
        if not tname:
            continue

        columns: list[ColumnInfo] = []
        constraints: list[str] = []
        table_level_pk: list[str] = []

        for e in schema_node.expressions:
            if isinstance(e, exp.ColumnDef):
                is_pk = any(
                    isinstance(c.kind, exp.PrimaryKeyColumnConstraint) for c in e.constraints
                )
                not_null = any(
                    isinstance(c.kind, exp.NotNullColumnConstraint) for c in e.constraints
                )
                is_unique = any(
                    isinstance(c.kind, exp.UniqueColumnConstraint) for c in e.constraints
                )
                columns.append(
                    ColumnInfo(
                        name=e.name,
                        type=e.kind.sql() if e.kind else "unknown",
                        required=not_null or is_pk,
                        primary_key=is_pk,
                    )
                )
                if is_pk:
                    constraints.append(f"PRIMARY KEY ({e.name})")
                if is_unique:
                    constraints.append(f"UNIQUE ({e.name})")
            elif isinstance(e, exp.PrimaryKey):
                cols = [getattr(c, "name", c.sql()) for c in e.expressions]
                table_level_pk.extend(cols)
                constraints.append(f"PRIMARY KEY ({', '.join(cols)})")
            else:
                # Table-level constraint (FK / UNIQUE / CHECK / named CONSTRAINT): keep its SQL.
                constraints.append(" ".join(e.sql().split()))

        if table_level_pk:
            pk_set = set(table_level_pk)
            for col in columns:
                if col.name in pk_set:
                    col.required = True
                    col.primary_key = True

        tables.append(TableInfo(name=tname, columns=columns, constraints=constraints))
    return tables


def summarize_ddl_file(path: str) -> DBSummary:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"DDL file not found: {path}")
    tables = parse_ddl(p.read_text(encoding="utf-8"))
    if not tables:
        raise ValueError(f"No CREATE TABLE statements found in {path}.")
    return DBSummary(
        system_name=f"{p.stem} (DDL schema)",
        base_url=str(p),
        dialect="sql",
        source_kind="ddl",
        tables=tables[:MAX_TABLES],
        truncated=len(tables) > MAX_TABLES,
    )


# --------------------------------------------------------------------------------------
# Deterministic element enumeration
# --------------------------------------------------------------------------------------


def elements_from_db_summary(summary: DBSummary) -> list[TestableElement]:
    """One `table` element per table: columns → `column` inputs, constraints → preconditions."""
    elements: list[TestableElement] = []
    seen: set[str] = set()
    for t in summary.tables:
        qualified = f"{t.schema}.{t.name}" if t.schema else t.name
        inputs = [
            InputField(name=c.name, type=c.type, required=c.required, where="column")
            for c in t.columns[:MAX_COLUMNS]
        ]
        pk = [c.name for c in t.columns if c.primary_key]
        desc = f"Table {qualified} ({len(t.columns)} columns"
        desc += f", PK {', '.join(pk)})." if pk else ")."
        elements.append(
            TestableElement(
                kind="table",
                name=_unique(t.name, seen),
                location=qualified,
                description=desc,
                inputs=inputs,
                preconditions=t.constraints,
                priority="medium",
            )
        )
    return elements


# --------------------------------------------------------------------------------------
# LLM judgement layer
# --------------------------------------------------------------------------------------


_DB_JUDGMENT_SYSTEM = """\
You are a senior test-automation analyst. You are given the COMPLETE, authoritative list of
`table` elements reflected from a database schema (columns as inputs, constraints as
preconditions). The list is exhaustive and correct: you must NOT add, remove, rename, or
invent tables or columns.

Your job is judgement only:
- Prioritise by exception: list the HIGH-priority and LOW-priority table names (exact names;
  omit mediums). Tables central to the domain — rich constraints, referenced by many foreign
  keys — are usually high; small lookup/enum tables are usually low.
- auth_strategy is null for a database schema.
- Write a concise system summary.
- Propose 5-10 CONTRACT-check journeys (e.g. "the orders table enforces its NOT NULL and
  foreign-key constraints"), referencing table element names EXACTLY. Do not invent data flows.
"""


def build_db_judgment_prompt(summary: DBSummary, elements: list[TestableElement]) -> str:
    return (
        f"Database: {summary.system_name} (dialect {summary.dialect}, via {summary.source_kind})\n"
        f"Testable elements ({len(elements)}) — this list is complete and authoritative:\n"
        f"{render_elements_for_prompt(elements)}\n\n"
        "Provide the HIGH-priority and LOW-priority table element names (by exact name; omit "
        "mediums), the auth_strategy, a system summary, and 5-10 suggested contract-check "
        "journeys referencing these element names."
    )


async def discover_db(source: str, provider: LLMProvider) -> CoverageInventory:
    """Database discovery: reflect a live DB *or* parse a .sql DDL file -> enumerate `table`
    elements deterministically -> LLM supplies judgement -> merge into a validated inventory."""
    if source.endswith(".sql") or (Path(source).is_file() and "://" not in source):
        summary = summarize_ddl_file(source)
    elif _is_connection_url(source):
        summary = reflect_database(source)
    else:
        raise ValueError(
            "DB source must be a connection URL (e.g. postgresql://user@host/db, "
            "sqlite:///path.db) or a .sql DDL file."
        )

    elements = elements_from_db_summary(summary)
    if not elements:
        raise ValueError("No tables discovered; nothing to test.")
    names = {e.name for e in elements}

    judgment = await provider.generate_structured(
        build_db_judgment_prompt(summary, elements),
        InventoryJudgment,
        system=_DB_JUDGMENT_SYSTEM,
        label="discover.db",
    )

    priorities = judgment.priority_map(names)
    for el in elements:
        el.priority = priorities.get(el.name, el.priority)
    for journey in judgment.suggested_journeys:
        journey.elements = [n for n in journey.elements if n in names]

    return CoverageInventory(
        system_name=summary.system_name,
        base_url=summary.base_url,
        source="db_schema",
        auth_strategy=None,  # a schema has no auth surface
        summary=judgment.system_summary,
        elements=elements,
        suggested_journeys=judgment.suggested_journeys,
    )

"""Rebuild a table's shape by replaying SQL migrations in order.

Supports the statements migrations actually use: CREATE TABLE, DROP TABLE,
ALTER TABLE ... ADD / DROP / RENAME COLUMN, ALTER COLUMN TYPE, SET / DROP NOT
NULL, RENAME TO, and ADD PRIMARY KEY. Anything else is skipped.
"""

from __future__ import annotations

import logging
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

from ..snapshot import Snapshot
from ..types import normalize_type
from . import Column, Extraction, TableSchema

# sqlglot logs "unsupported syntax" warnings for valid Postgres it only half
# understands; findings come from the replay, not from its log.
logging.getLogger("sqlglot").setLevel(logging.ERROR)

_ALTER = getattr(exp, "Alter", None) or getattr(exp, "AlterTable")


def _ident(node: exp.Expression | str | None) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, exp.Identifier):
        return node.this if node.quoted else node.this.lower()
    if isinstance(node, exp.Column):
        return _ident(node.this)
    return node.name.lower()


def _table_name(table: exp.Expression | None) -> str:
    if table is None:
        return ""
    if isinstance(table, exp.Schema):
        table = table.this
    if not isinstance(table, exp.Table):
        return _ident(table)
    parts = [_ident(table.args.get("catalog")), _ident(table.args.get("db")), _ident(table.this)]
    return ".".join(p for p in parts if p)


def _split_name(name: str) -> tuple[str, str]:
    name = name.strip().strip('"')
    if "." in name:
        schema, _, table = name.rpartition(".")
        return schema.split(".")[-1].strip('"').lower(), table.strip('"')
    return "", name


def same_table(a: str, b: str) -> bool:
    """Match ``public.t`` with ``t``: Postgres resolves unqualified names to ``public``."""
    sa, ta = _split_name(a)
    sb, tb = _split_name(b)
    if ta.lower() != tb.lower():
        return False
    defaults = {"", "public", "dbo", "main"}
    return sa == sb or (sa in defaults and sb in defaults)


def _raw_type(kind: exp.DataType | None) -> str:
    if kind is None:
        return ""
    return kind.sql(dialect="postgres")


def _column_from_def(cdef: exp.ColumnDef) -> Column:
    kind = cdef.args.get("kind")
    raw = _raw_type(kind)
    nullable = True
    primary = False
    for constraint in cdef.args.get("constraints") or []:
        ckind = constraint.args.get("kind")
        if isinstance(ckind, exp.NotNullColumnConstraint):
            nullable = bool(ckind.args.get("allow"))
        elif isinstance(ckind, exp.PrimaryKeyColumnConstraint):
            primary = True
            nullable = False
    norm = normalize_type(raw)
    if norm == "unknown" and kind is not None and kind.this == exp.DataType.Type.USERDEFINED:
        norm = normalize_type(kind.args.get("kind") or raw)
    return Column(name=_ident(cdef.this), type=norm, raw_type=raw, nullable=nullable, primary_key=primary)


def _statements(text: str, dialect: str, origin: str, warnings: list[str]) -> list[exp.Expression]:
    try:
        return [s for s in sqlglot.parse(text, read=dialect) if s is not None]
    except (ParseError, TokenError):
        pass
    # One bad statement should not hide the rest of the file: parse them one by one.
    out: list[exp.Expression] = []
    for chunk in re.split(r";\s*(?:\n|$)", text):
        if not chunk.strip():
            continue
        try:
            out.extend(s for s in sqlglot.parse(chunk, read=dialect) if s is not None)
        except (ParseError, TokenError):
            first = chunk.strip().splitlines()[0][:80]
            warnings.append(f"{origin}: skipped a statement SchemaBot could not parse: {first}")
    return out


class _Replay:
    def __init__(self) -> None:
        self.tables: dict[str, TableSchema] = {}

    def find(self, name: str) -> str | None:
        return next((key for key in self.tables if same_table(key, name)), None)

    def apply(self, stmt: exp.Expression, origin: str) -> None:
        if isinstance(stmt, exp.Create) and (stmt.args.get("kind") or "").upper() == "TABLE":
            self._create(stmt, origin)
        elif isinstance(stmt, exp.Drop) and (stmt.args.get("kind") or "").upper() == "TABLE":
            for table in stmt.args.get("tables") or []:
                key = self.find(_table_name(table))
                if key:
                    del self.tables[key]
        elif isinstance(stmt, _ALTER) and (stmt.args.get("kind") or "TABLE").upper() == "TABLE":
            self._alter(stmt, origin)

    def _create(self, stmt: exp.Create, origin: str) -> None:
        schema = stmt.this
        if not isinstance(schema, exp.Schema):
            return  # CREATE TABLE ... AS SELECT: shape unknown statically
        name = _table_name(schema)
        existing = self.find(name)
        if existing and stmt.args.get("exists"):
            return  # CREATE TABLE IF NOT EXISTS on an existing table is a no-op
        table = TableSchema(name=name, origin=origin)
        pk_cols: list[str] = []
        for item in schema.expressions:
            if isinstance(item, exp.ColumnDef):
                col = _column_from_def(item)
                table.columns[col.name] = col
            elif isinstance(item, exp.PrimaryKey):
                pk_cols.extend(_ident(e) for e in item.expressions)
            elif isinstance(item, exp.Constraint):
                for inner in item.expressions:
                    if isinstance(inner, exp.PrimaryKey):
                        pk_cols.extend(_ident(e) for e in inner.expressions)
        for col_name in pk_cols:
            if col_name in table.columns:
                table.columns[col_name].primary_key = True
                table.columns[col_name].nullable = False
        if existing:
            del self.tables[existing]
        self.tables[name] = table

    def _alter(self, stmt: exp.Expression, origin: str) -> None:
        key = self.find(_table_name(stmt.this))
        if key is None:
            return
        table = self.tables[key]
        table.origin = origin
        for action in stmt.args.get("actions") or []:
            if isinstance(action, exp.ColumnDef):
                col = _column_from_def(action)
                if not (action.args.get("exists") and col.name in table.columns):
                    table.columns[col.name] = col
            elif isinstance(action, exp.Drop) and (action.args.get("kind") or "").upper() == "COLUMN":
                for target in action.args.get("tables") or [action.this]:
                    table.columns.pop(_ident(target), None)
            elif isinstance(action, exp.RenameColumn):
                old, new = _ident(action.this), _ident(action.args.get("to"))
                if old in table.columns:
                    table.columns[old].name = new
                    # keep the column's position
                    table.columns = {(new if k == old else k): c for k, c in table.columns.items()}
            elif isinstance(action, exp.AlterColumn):
                col = table.columns.get(_ident(action.this))
                if col is None:
                    continue
                dtype = action.args.get("dtype")
                if dtype is not None:
                    col.raw_type = _raw_type(dtype)
                    col.type = normalize_type(col.raw_type)
                allow_null = action.args.get("allow_null")
                if allow_null is not None:
                    col.nullable = bool(allow_null)
            elif type(action).__name__ in ("AlterRename", "RenameTable"):
                new_name = _table_name(action.this)
                del self.tables[key]
                table.name = new_name
                self.tables[new_name] = table
                key = new_name
            elif isinstance(action, exp.AddConstraint):
                for constraint in action.expressions:
                    for inner in getattr(constraint, "expressions", []) or []:
                        if isinstance(inner, exp.PrimaryKey):
                            for e in inner.expressions:
                                col = table.columns.get(_ident(e))
                                if col:
                                    col.primary_key = True
                                    col.nullable = False


def extract_table(snapshot: Snapshot, location: str, table: str, dialect: str = "postgres") -> Extraction:
    """Replay every ``.sql`` file under ``location`` (sorted by path) and return ``table``.

    ``location`` may be a directory of migrations or a single schema file.
    """
    warnings: list[str] = []
    files = [f for f in snapshot.list_files(location) if f.endswith(".sql")]
    if not files:
        return Extraction(None, [f"no .sql files found at {location}"])
    replay = _Replay()
    for path in files:
        text = snapshot.read_text(path) or ""
        for stmt in _statements(text, dialect, path, warnings):
            replay.apply(stmt, path)
    key = replay.find(table)
    return Extraction(replay.tables[key] if key else None, warnings)

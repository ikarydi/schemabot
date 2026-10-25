"""Schema extractors: read a source's current shape from code in the repo.

Extractors are offline and static. They parse migrations and model files; they
never connect to a database or import user code.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..contract import Source
from ..snapshot import Snapshot


@dataclass
class Column:
    name: str
    type: str  # normalized
    raw_type: str
    nullable: bool = True
    primary_key: bool = False


@dataclass
class TableSchema:
    name: str
    columns: dict[str, Column] = field(default_factory=dict)
    origin: str = ""  # file the shape was read from

    def column(self, name: str) -> Column | None:
        return self.columns.get(name)


@dataclass
class Extraction:
    schema: TableSchema | None
    warnings: list[str] = field(default_factory=list)


def extract(snapshot: Snapshot, source: Source) -> Extraction:
    if source.type in ("postgres", "sql"):
        from .sql import extract_table

        return extract_table(snapshot, source.schema_from, source.table or "", source.dialect)
    if source.type == "pydantic":
        from .pydantic_model import extract_model

        return extract_model(snapshot, source.schema_from, source.model or "")
    return Extraction(None, [f"unsupported source type {source.type!r}"])

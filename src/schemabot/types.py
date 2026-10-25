"""Normalized column types shared by contracts and extractors.

Every source (SQL DDL, Pydantic models, contract YAML) is mapped onto one small
vocabulary so that a `TEXT` column, a `str` field and a contract `type: string`
compare as equal.
"""

from __future__ import annotations

import re

STRING = "string"
INTEGER = "integer"
FLOAT = "float"
DECIMAL = "decimal"
BOOLEAN = "boolean"
TIMESTAMP = "timestamp"
DATE = "date"
TIME = "time"
UUID = "uuid"
JSON = "json"
BINARY = "binary"
ARRAY = "array"
VECTOR = "vector"
UNKNOWN = "unknown"

_ALIASES: dict[str, str] = {}


def _alias(target: str, *names: str) -> None:
    for name in names:
        _ALIASES[name] = target


_alias(
    STRING,
    "string", "str", "text", "varchar", "char", "character", "nchar", "nvarchar",
    "character varying", "bpchar", "citext", "name", "enum", "emailstr", "httpurl",
    "anyurl", "literal", "mediumtext", "longtext", "tinytext", "clob", "inet", "cidr",
)
_alias(
    INTEGER,
    "integer", "int", "int2", "int4", "int8", "smallint", "bigint", "tinyint",
    "mediumint", "serial", "bigserial", "smallserial", "long", "short", "uint",
)
_alias(FLOAT, "float", "float4", "float8", "double", "double precision", "real")
_alias(DECIMAL, "decimal", "numeric", "number", "money", "bignumeric")
_alias(BOOLEAN, "boolean", "bool", "bit")
_alias(
    TIMESTAMP,
    "timestamp", "timestamptz", "timestampltz", "timestampntz", "datetime",
    "datetime64", "timestamp with time zone", "timestamp without time zone",
    "awaredatetime", "naivedatetime",
)
_alias(DATE, "date")
_alias(TIME, "time", "timetz", "time with time zone", "time without time zone")
_alias(UUID, "uuid", "uniqueidentifier", "guid")
_alias(JSON, "json", "jsonb", "dict", "mapping", "object", "variant", "struct", "map", "hstore")
_alias(BINARY, "binary", "varbinary", "blob", "bytea", "bytes", "longblob", "image")
_alias(ARRAY, "array", "list", "tuple", "set", "sequence", "frozenset")
_alias(VECTOR, "vector", "halfvec", "sparsevec", "embedding")


def normalize_type(raw: str | None) -> str:
    """Map a type name from any source onto the normalized vocabulary.

    Parameters and array suffixes are stripped first, so ``VARCHAR(255)``,
    ``vector(1536)`` and ``TEXT[]`` resolve to ``string``, ``vector`` and ``array``.
    """
    if not raw:
        return UNKNOWN
    text = raw.strip().lower()
    if text.endswith("[]") or text.startswith("array<") or text.startswith("array("):
        return ARRAY
    text = re.sub(r"\(.*\)$", "", text).strip()
    text = text.split(".")[-1]
    if text in _ALIASES:
        return _ALIASES[text]
    if text.startswith("timestamp"):
        return TIMESTAMP
    if text.startswith("time"):
        return TIME
    if text.startswith("int") or text.startswith("uint"):
        return INTEGER
    return UNKNOWN


def compatible(contract_type: str, actual_type: str) -> bool:
    """True when an actual column type satisfies the contract's declared type.

    Unknown types on either side are treated as compatible: SchemaBot only
    reports a mismatch it can prove.
    """
    if UNKNOWN in (contract_type, actual_type):
        return True
    return contract_type == actual_type

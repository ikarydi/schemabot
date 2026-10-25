import pytest

from schemabot.types import compatible, normalize_type


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("TEXT", "string"),
        ("VARCHAR(255)", "string"),
        ("character varying", "string"),
        ("BIGINT", "integer"),
        ("int8", "integer"),
        ("TIMESTAMPTZ", "timestamp"),
        ("timestamp with time zone", "timestamp"),
        ("UUID", "uuid"),
        ("JSONB", "json"),
        ("TEXT[]", "array"),
        ("vector(1536)", "vector"),
        ("NUMERIC(10, 2)", "decimal"),
        ("str", "string"),
        ("datetime", "timestamp"),
        ("", "unknown"),
        ("geography", "unknown"),
    ],
)
def test_normalize(raw, expected):
    assert normalize_type(raw) == expected


def test_compatible_treats_unknown_as_ok():
    assert compatible("string", "string")
    assert not compatible("string", "integer")
    assert compatible("unknown", "integer")
    assert compatible("uuid", "unknown")

from schemabot.extract.sql import extract_table, same_table
from schemabot.snapshot import WorkingTree


def tree(tmp_path, files: dict[str, str]) -> WorkingTree:
    for name, text in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return WorkingTree(tmp_path)


def test_replays_migrations_in_order(tmp_path):
    snap = tree(
        tmp_path,
        {
            "m/001.sql": "CREATE TABLE t (id UUID PRIMARY KEY, a TEXT NOT NULL, b INT);",
            "m/002.sql": """
                ALTER TABLE t ADD COLUMN c JSONB;
                ALTER TABLE t DROP COLUMN b;
                ALTER TABLE t RENAME COLUMN a TO title;
                ALTER TABLE t ALTER COLUMN c SET NOT NULL;
                ALTER TABLE t ALTER COLUMN title TYPE VARCHAR(200);
                ALTER TABLE t ALTER COLUMN title DROP NOT NULL;
            """,
        },
    )
    schema = extract_table(snap, "m", "public.t").schema
    assert schema is not None
    assert list(schema.columns) == ["id", "title", "c"]
    assert schema.columns["id"].primary_key and not schema.columns["id"].nullable
    assert schema.columns["title"].type == "string" and schema.columns["title"].nullable
    assert schema.columns["c"].type == "json" and not schema.columns["c"].nullable
    assert schema.origin == "m/002.sql"


def test_table_level_primary_key_and_rename_and_drop(tmp_path):
    snap = tree(
        tmp_path,
        {
            "s.sql": """
                CREATE TABLE app.docs (id BIGINT, body TEXT, PRIMARY KEY (id));
                CREATE TABLE app.old (x INT);
                DROP TABLE app.old;
                ALTER TABLE app.docs RENAME TO documents;
            """
        },
    )
    assert extract_table(snap, "s.sql", "app.old").schema is None
    assert extract_table(snap, "s.sql", "app.docs").schema is None
    docs = extract_table(snap, "s.sql", "documents").schema
    assert docs is not None and docs.columns["id"].primary_key


def test_create_if_not_exists_does_not_reset_table(tmp_path):
    snap = tree(
        tmp_path,
        {
            "001.sql": "CREATE TABLE t (id INT); ALTER TABLE t ADD COLUMN extra TEXT;",
            "002.sql": "CREATE TABLE IF NOT EXISTS t (id INT);",
        },
    )
    assert "extra" in extract_table(snap, ".", "t").schema.columns


def test_unparseable_statement_is_skipped_with_warning(tmp_path):
    snap = tree(
        tmp_path,
        {"001.sql": "CREATE TABLE t (id INT);\nTHIS IS NOT SQL AT ALL (((;\nALTER TABLE t ADD COLUMN y TEXT;\n"},
    )
    result = extract_table(snap, ".", "t")
    assert set(result.schema.columns) == {"id", "y"}
    assert result.warnings


def test_quoted_identifiers_keep_case(tmp_path):
    snap = tree(tmp_path, {"001.sql": 'CREATE TABLE "Docs" ("Title" TEXT, body TEXT);'})
    schema = extract_table(snap, ".", '"Docs"').schema
    assert set(schema.columns) == {"Title", "body"}


def test_missing_location(tmp_path):
    result = extract_table(WorkingTree(tmp_path), "nowhere", "t")
    assert result.schema is None and result.warnings


def test_same_table():
    assert same_table("public.t", "t")
    assert same_table("t", "T")
    assert not same_table("billing.t", "t")
    assert same_table("billing.t", "billing.t")

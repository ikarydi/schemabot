import textwrap

from schemabot.scan import scan_code, scan_query
from schemabot.snapshot import WorkingTree

FIELDS = {"id", "tenant_id", "subject", "body", "customer_email"}


def scan(tmp_path, code: str):
    (tmp_path / "ingest.py").write_text(textwrap.dedent(code))
    refs, warnings = scan_code(WorkingTree(tmp_path), ["ingest.py"], FIELDS)
    return {r.field for r in refs}, refs


def test_direct_attribute_and_subscript(tmp_path):
    fields, _ = scan(
        tmp_path,
        """
        def run(store, rows):
            store.add_texts([r.subject + r["body"] for r in rows], metadatas=[{"id": r.id} for r in rows])
        """,
    )
    assert fields == {"subject", "body"}  # metadata is not embedded


def test_follows_locals_and_helper_returns(tmp_path):
    fields, refs = scan(
        tmp_path,
        """
        def to_text(t):
            header = f"{t.subject}"
            return header + "\\n" + t.get("customer_email")

        def run(embedder, tickets):
            texts = []
            for t in tickets:
                texts.append(to_text(t))
            return embedder.embed_documents(texts)
        """,
    )
    assert fields == {"subject", "customer_email"}
    assert {r.sink for r in refs} == {"embed_documents"}


def test_parameters_passed_to_a_sink_make_the_function_a_sink(tmp_path):
    fields, refs = scan(
        tmp_path,
        """
        def push(client, texts):
            return client.embeddings.create(model="m", input=texts)

        def run(client, rows):
            push(client, [r.body for r in rows])
            push(client, texts=[r.customer_email for r in rows])
        """,
    )
    assert fields == {"body", "customer_email"}
    assert {r.sink for r in refs} == {"push()"}


def test_document_page_content_but_not_metadata(tmp_path):
    fields, _ = scan(
        tmp_path,
        """
        from langchain_core.documents import Document
        docs = [Document(page_content=row.body, metadata={"email": row.customer_email}) for row in rows]
        """,
    )
    assert fields == {"body"}


def test_select_in_string_literal(tmp_path):
    fields, _ = scan(
        tmp_path,
        """
        def run(conn, model):
            rows = conn.execute("SELECT subject, customer_email FROM tickets").fetchall()
            model.encode([" ".join(r) for r in rows])
        """,
    )
    assert fields == {"subject", "customer_email"}


def test_no_sink_no_refs(tmp_path):
    fields, _ = scan(tmp_path, "def f(r):\n    print(r.customer_email)\n")
    assert fields == set()


def test_syntax_error_warns(tmp_path):
    (tmp_path / "ingest.py").write_text("def broken(:\n")
    refs, warnings = scan_code(WorkingTree(tmp_path), ["ingest.py"], FIELDS)
    assert refs == [] and warnings


def test_scan_query():
    q = scan_query("SELECT t.subject, lower(body) AS b FROM tickets t WHERE tenant_id = $1")
    assert q.fields == {"subject", "body", } and not q.star
    assert scan_query("SELECT * FROM tickets").star
    assert scan_query("DELETE FROM tickets").error

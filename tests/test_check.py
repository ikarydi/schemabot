"""End-to-end: a base commit of the demo project, then a pull request's changes."""

import shutil

from schemabot.check import run_check
from schemabot.findings import Severity
from schemabot.snapshot import GitRevision, WorkingTree

from .conftest import git


def check(repo, base="main"):
    return run_check(WorkingTree(repo.root), "contracts", GitRevision(repo.root, base))


def rules(result, include_preexisting=False):
    return {
        (f.rule, f.field)
        for f in result.findings
        if include_preexisting or not f.preexisting
    }


def test_unchanged_demo_passes(repo):
    result = check(repo)
    assert not result.fails(Severity.WARNING)
    assert result.worst() is None


def test_validate_mode_has_no_base(repo):
    result = run_check(WorkingTree(repo.root), "contracts")
    assert not result.fails(Severity.BREAKING)
    assert all(r.status == "checked" for r in result.results)


def test_renaming_a_filter_key_is_breaking_and_suggests_the_rename(repo):
    repo.write("migrations/003.sql", "ALTER TABLE public.support_tickets RENAME COLUMN tenant_id TO org_id;\n")
    result = check(repo)
    assert result.fails(Severity.BREAKING)
    [finding] = [f for f in result.findings if f.rule == "filter-key-missing"]
    assert finding.field == "tenant_id"
    assert "org_id" in finding.hint
    assert finding.file == "migrations/003.sql"


def test_embedding_a_pii_field_is_breaking_with_line(repo):
    repo.replace("ingest/tickets.py", "{ticket['body']}\"", "{ticket['body']}\\n{ticket['customer_email']}\"")
    result = check(repo)
    [finding] = [f for f in result.findings if f.rule == "never-embed-violation"]
    assert finding.field == "customer_email"
    assert finding.file == "ingest/tickets.py" and finding.line
    assert result.fails(Severity.BREAKING)


def test_classified_pii_is_protected_without_never_embed(repo):
    repo.replace("ingest/kb.py", '{a.content}"', '{a.content}\\n{a.author_email}"')
    result = check(repo)
    assert ("never-embed-violation", "author_email") in rules(result)


def test_embedding_an_undeclared_field_warns(repo):
    repo.replace("ingest/tickets.py", "{ticket['body']}\"", "{ticket['body']}\\n{ticket['updated_at']}\"")
    result = check(repo)
    assert ("embed-scope-exceeded", "updated_at") in rules(result)
    assert result.worst() == Severity.WARNING


def test_changing_the_embedding_model_requires_reindex(repo):
    repo.replace("contracts/support_tickets.yaml", "text-embedding-3-small", "text-embedding-3-large")
    result = check(repo)
    assert result.worst() == Severity.REINDEX
    assert not result.fails(Severity.BREAKING)
    assert result.fails(Severity.REINDEX)
    [f] = [f for f in result.findings if f.rule == "index-changed"]
    assert "text-embedding-3-large" in f.message


def test_changing_embed_scope_requires_reindex(repo):
    repo.replace("contracts/support_tickets.yaml", "embed: [subject, body]", "embed: [subject]")
    assert ("embed-scope-changed", None) in rules(check(repo))


def test_type_and_nullability_changes(repo):
    repo.write(
        "migrations/003.sql",
        "ALTER TABLE public.support_tickets ALTER COLUMN body DROP NOT NULL;\n"
        "ALTER TABLE public.support_tickets ALTER COLUMN subject TYPE INT USING 0;\n",
    )
    found = rules(check(repo))
    assert ("nullability-mismatch", "body") in found
    assert ("type-mismatch", "subject") in found


def test_new_pii_looking_column_warns(repo):
    repo.write("migrations/003.sql", "ALTER TABLE public.support_tickets ADD COLUMN phone_number TEXT;\n")
    found = rules(check(repo))
    assert ("possible-pii", "phone_number") in found


def test_pydantic_source_drift(repo):
    repo.replace("app/models.py", "    workspace_id: UUID\n", "")
    found = rules(check(repo))
    assert ("filter-key-missing", "workspace_id") in found


def test_contract_evolution(repo):
    repo.replace("contracts/support_tickets.yaml", "  filterKeys: [tenant_id]          # the retriever must filter on these\n", "")
    repo.replace("contracts/support_tickets.yaml", "  neverEmbed: [customer_email]     # fails the PR if it reaches the embedder\n", "")
    repo.replace("contracts/support_tickets.yaml", "classification: pii", "classification: internal")
    repo.replace("contracts/support_tickets.yaml", "{name: updated_at,     type: timestamp, required: true}", "{name: updated_at, type: timestamp}")
    found = rules(check(repo))
    assert ("filter-key-removed", "tenant_id") in found
    assert ("never-embed-relaxed", "customer_email") in found
    assert ("contract-guarantee-relaxed", "updated_at") in found
    assert ("no-filter-keys", None) in found


def test_preexisting_problems_do_not_block(repo):
    repo.write("migrations/003.sql", "ALTER TABLE public.support_tickets ALTER COLUMN body DROP NOT NULL;\n")
    repo.commit("already broken on main")
    repo.write("README.txt", "unrelated change\n")
    result = check(repo)
    assert not result.fails(Severity.BREAKING)
    [f] = [f for f in result.findings if f.rule == "nullability-mismatch"]
    assert f.preexisting


def test_invalid_and_removed_contracts(repo):
    repo.write("contracts/broken.yaml", "id: broken\nsource: {type: postgres}\nschema: []\n")
    repo.delete("contracts/kb_articles.yaml")
    result = check(repo)
    statuses = {r.contract_id: r.status for r in result.results}
    assert statuses["contracts/broken.yaml"] == "invalid"
    assert statuses["kb-articles"] == "removed"
    assert result.fails(Severity.BREAKING)


def test_contract_level_policy(repo):
    repo.replace("contracts/kb_articles.yaml", "embed: [title, content]", "embed: [title, content, author_email]")
    found = rules(check(repo))
    assert ("pii-in-embed-scope", "author_email") in found


def test_embed_query_star(repo):
    repo.replace(
        "contracts/support_tickets.yaml",
        "  code: [ingest/tickets.py]        # where SchemaBot looks for embed calls\n",
        "  embedQuery: SELECT * FROM public.support_tickets\n",
    )
    assert ("embed-query-star", None) in rules(check(repo))


def test_subdirectory_root(repo, tmp_path):
    # The project lives in a sub-folder of the repository.
    outer = tmp_path / "outer"
    outer.mkdir()
    git(outer, "init", "-q", "-b", "main")
    shutil.copytree(repo.root, outer / "svc", ignore=shutil.ignore_patterns(".git"))
    git(outer, "add", "-A")
    git(outer, "commit", "-qm", "base")
    (outer / "svc" / "migrations" / "003.sql").write_text(
        "ALTER TABLE public.support_tickets DROP COLUMN tenant_id;\n"
    )
    result = run_check(WorkingTree(outer / "svc"), "contracts", GitRevision(outer, "main", "svc"))
    assert ("filter-key-missing", "tenant_id") in rules(result)

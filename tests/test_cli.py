import json

from schemabot.cli import main


def test_check_exit_codes_and_outputs(repo, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(repo.root)
    assert main(["check", "--base", "main"]) == 0
    repo.write("migrations/003.sql", "ALTER TABLE public.support_tickets DROP COLUMN tenant_id;\n")
    md, js, sarif = tmp_path / "r.md", tmp_path / "r.json", tmp_path / "r.sarif"
    code = main(["check", "--base", "main", "--markdown", str(md), "--json", str(js), "--sarif", str(sarif)])
    assert code == 1
    out = capsys.readouterr().out
    assert "filter-key-missing" in out and out.strip().endswith("FAIL")
    assert md.read_text().startswith("<!-- schemabot-report -->")
    assert json.loads(js.read_text())["passed"] is False
    doc = json.loads(sarif.read_text())
    assert doc["runs"][0]["results"][0]["ruleId"] == "filter-key-missing"


def test_fail_on_never_and_reindex(repo, monkeypatch):
    monkeypatch.chdir(repo.root)
    repo.replace("contracts/support_tickets.yaml", "dimension: 1536", "dimension: 3072")
    assert main(["check", "--base", "main"]) == 0
    assert main(["check", "--base", "main", "--fail-on", "reindex"]) == 1
    assert main(["check", "--base", "main", "--fail-on", "never"]) == 0


def test_formats(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo.root)
    for fmt in ("text", "markdown", "json", "sarif"):
        assert main(["check", "--format", fmt]) == 0
    assert '"version": "2.1.0"' in capsys.readouterr().out


def test_usage_errors(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo.root)
    assert main(["check", "--base", "does-not-exist"]) == 2
    assert main(["check", "--contracts", "missing"]) == 2
    assert main(["validate"]) == 0
    assert "not found" in capsys.readouterr().err

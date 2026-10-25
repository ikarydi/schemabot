import sys
import types

from schemabot.check import run_check
from schemabot.contract import load_contracts
from schemabot.explain import explain
from schemabot.snapshot import GitRevision, WorkingTree


def broken(repo):
    repo.write("migrations/003.sql", "ALTER TABLE public.support_tickets DROP COLUMN tenant_id;\n")
    result = run_check(WorkingTree(repo.root), "contracts", GitRevision(repo.root, "main"))
    contracts = {c.id: c for c in load_contracts(WorkingTree(repo.root), "contracts")[0]}
    return result, contracts


def test_no_findings_means_no_call(repo):
    result = run_check(WorkingTree(repo.root), "contracts", GitRevision(repo.root, "main"))
    assert explain(result, {}) == (None, None)


def fake_anthropic(create):
    class Client:
        def __init__(self):
            self.beta = types.SimpleNamespace(messages=types.SimpleNamespace(create=create))

    return types.SimpleNamespace(
        Anthropic=Client,
        APIStatusError=type("APIStatusError", (Exception,), {}),
        APIConnectionError=type("APIConnectionError", (Exception,), {}),
    )


def test_sdk_not_installed_is_reported_not_raised(repo, monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)  # makes `import anthropic` fail
    summary, error = explain(*broken(repo))
    assert summary is None and "schemabot[llm]" in error


def test_missing_key_is_reported_not_raised(repo, monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(lambda **kw: None))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    summary, error = explain(*broken(repo))
    assert summary is None and "ANTHROPIC_API_KEY" in error


def test_calls_claude_with_fallbacks(repo, monkeypatch):
    calls = {}

    class Block:
        type = "text"
        text = "Dropping `tenant_id` breaks tenant isolation."

    class Response:
        stop_reason = "end_turn"
        content = [Block()]

    def create(**kwargs):
        calls.update(kwargs)
        return Response()

    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic(create))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.delenv("SCHEMABOT_MODEL", raising=False)
    summary, error = explain(*broken(repo))
    assert error is None and "tenant isolation" in summary
    assert calls["model"] == "claude-opus-5-5"
    assert calls["fallbacks"] == "default"
    assert "tenant_id" in calls["messages"][0]["content"]

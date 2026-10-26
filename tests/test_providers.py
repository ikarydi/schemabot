"""Phase 2: provider and region detection, the lineage graph and the rules over it."""

import json
import textwrap

from schemabot.cli import main
from schemabot.findings import Severity
from schemabot.providers import region_allowed, region_zone, scan_providers
from schemabot.snapshot import WorkingTree

from .test_check import check


def found(result):
    return {(f.rule, f.contract, f.field, f.system, f.provider) for f in result.findings if not f.preexisting}


def scan(tmp_path, code):
    (tmp_path / "p.py").write_text(textwrap.dedent(code))
    return scan_providers(WorkingTree(tmp_path), ["p.py"])


def test_detects_clients_regions_models_and_training(tmp_path):
    providers, training, warnings = scan(
        tmp_path,
        """
        import boto3, cohere, openai
        from anthropic import Anthropic
        from langchain_openai import OpenAIEmbeddings
        from transformers import Trainer

        a = Anthropic()
        e = OpenAIEmbeddings(model="text-embedding-3-large")
        b = boto3.client("bedrock-runtime", region_name="us-east-1")
        s3 = boto3.client("s3")
        c = cohere.ClientV2()
        openai.embeddings.create(input=["x"])
        openai.files.create(file=b"")
        client.fine_tuning.jobs.create(training_file="f", model="gpt-4o-mini")
        Trainer(model=None)
        """,
    )
    got = {(p.provider, p.via, p.model, p.region) for p in providers}
    assert got == {
        ("anthropic", "Anthropic", None, None),
        ("openai", "OpenAIEmbeddings", "text-embedding-3-large", None),
        ("bedrock", 'boto3.client("bedrock-runtime")', None, "us-east-1"),
        ("cohere", "cohere.ClientV2", None, None),
        ("openai", "openai.embeddings.create", None, None),
    }
    assert {t.via for t in training} == {"fine_tuning.jobs.create", "Trainer"}
    assert not warnings


def test_region_zones():
    assert region_zone("eu-west-1") == region_zone("europe-west4") == region_zone("westeurope") == "eu"
    assert region_zone("us-central1") == region_zone("eastus") == "us"
    assert region_allowed("eu-central-1", ["eu"])
    assert region_allowed("us-east-1", ["us-east-1"])
    assert not region_allowed("us-east-1", ["eu"])


def test_demo_lineage_is_clean(repo):
    assert check(repo).worst() is None


def test_region_outside_residency_is_breaking(repo):
    repo.replace("ai-systems.yaml", "role: embedding, model: text-embedding-3-small, region: eu", "role: embedding, model: text-embedding-3-small, region: us-east-1")
    result = check(repo)
    assert ("residency-violation", "support-tickets", None, "support-copilot", "openai") in found(result)
    assert result.fails(Severity.BREAKING)
    # kb-articles declares no residency, so it is not affected.
    assert not any(f.contract == "kb-articles" for f in result.findings if f.rule == "residency-violation")


def test_region_in_code_is_checked(repo):
    repo.replace("ingest/kb.py", "return OpenAI()", 'return OpenAI(base_url="https://x", region="us-west-2")')
    result = check(repo)
    [f] = [f for f in result.findings if f.rule == "residency-violation"]
    assert f.file == "ingest/kb.py" and f.line == 7


def test_undeclared_provider_and_region(repo):
    repo.write("ingest/rerank.py", "import cohere\n\nco = cohere.ClientV2()\n")
    got = found(check(repo))
    assert ("provider-undeclared", None, None, "support-copilot", "cohere") in got
    assert ("region-undeclared", "support-tickets", None, "support-copilot", "cohere") in got
    # cohere is not on the policy's allow-list for PII.
    assert ("provider-not-allowed", "support-tickets", "customer_email", "support-copilot", "cohere") in got


def test_contract_provider_allow_list(repo):
    repo.replace("contracts/kb_articles.yaml", "allowedUses: [rag, eval]", "allowedUses: [rag, eval]\n  allowedProviders: [openai]")
    got = found(check(repo))
    assert ("provider-not-allowed", "kb-articles", None, "support-copilot", "anthropic") in got
    assert ("provider-not-allowed", "kb-articles", None, "support-copilot", "openai") not in got


def test_provider_change_warns(repo):
    repo.replace("ai-systems.yaml", "text-embedding-3-small", "text-embedding-3-large")
    result = check(repo)
    [f] = [f for f in result.findings if f.rule == "provider-changed"]
    assert "text-embedding-3-large" in f.message and f.system == "support-copilot"
    assert result.worst() == Severity.WARNING


def test_undeclared_fine_tuning_is_breaking(repo):
    repo.write("ingest/tune.py", "def tune(client):\n    client.fine_tuning.jobs.create(training_file='f', model='m')\n")
    result = check(repo)
    [f] = [f for f in result.findings if f.rule == "undeclared-training"]
    assert f.file == "ingest/tune.py" and f.line == 2
    assert result.fails(Severity.BREAKING)


def test_declared_fine_tuning_system_covers_the_call(repo):
    repo.write("train/tune.py", "def tune(client):\n    client.fine_tuning.jobs.create(training_file='f', model='m')\n")
    repo.write(
        "ai-systems.yaml",
        repo.read("ai-systems.yaml")
        + "  - id: kb-tuner\n    use: finetune\n    purpose: docs-search\n    inputs: [kb-articles]\n"
        "    code: [train/]\n    providers: [{name: openai, role: training, region: eu}]\n    riskClass: minimal\n",
    )
    result = check(repo)
    assert not [f for f in result.findings if f.rule == "undeclared-training"]
    # The contract allows rag and eval only, and the policy forbids fine-tuning on PII.
    assert ("use-not-allowed", "kb-articles", None, "kb-tuner", None) in found(result)


def test_lineage_command(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo.root)
    assert main(["lineage"]) == 0
    out = capsys.readouterr().out
    assert "support-copilot (rag" in out and "-> openai" in out
    assert main(["lineage", "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["systems"][0]["inputs"][0]["contract"] == "support-tickets"
    assert main(["lineage", "--format", "mermaid"]) == 0
    assert capsys.readouterr().out.startswith("flowchart LR")
    repo.delete("ai-systems.yaml")
    assert main(["lineage"]) == 2

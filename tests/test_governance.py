"""Governance: the AI systems manifest, the policy pack and the rules that use them."""

import pytest

from schemabot.check import run_check
from schemabot.contract import ContractError, parse_contract
from schemabot.findings import Severity
from schemabot.governance import load_manifest
from schemabot.snapshot import WorkingTree

from .test_check import check, rules


def found(result):
    return {(f.rule, f.contract, f.field, f.system) for f in result.findings if not f.preexisting}


def test_demo_with_governance_passes(repo):
    result = check(repo)
    assert result.worst() is None
    assert not any(r.status == "systems" for r in result.results)


def test_purpose_outside_the_contract_is_breaking(repo):
    repo.replace("ai-systems.yaml", "purpose: agent-assist", "purpose: marketing")
    result = check(repo)
    assert ("purpose-not-allowed", "support-tickets", None, "support-copilot") in found(result)
    assert ("purpose-not-allowed", "kb-articles", None, "support-copilot") in found(result)
    assert result.fails(Severity.BREAKING)
    [f] = [f for f in result.findings if f.rule == "purpose-not-allowed" and f.contract == "support-tickets"]
    assert f.file == "ai-systems.yaml"
    assert "`support-search`, `agent-assist`" in f.message


def test_missing_purpose_is_reported(repo):
    repo.replace("ai-systems.yaml", "    purpose: agent-assist\n", "")
    assert ("purpose-not-allowed", "support-tickets", None, "support-copilot") in found(check(repo))


def test_fine_tuning_on_rag_only_data_is_breaking(repo):
    repo.write(
        "ai-systems.yaml",
        repo.read("ai-systems.yaml")
        + "  - id: ticket-tuner\n"
        "    use: finetune\n"
        "    purpose: agent-assist\n"
        "    inputs: [support-tickets]\n"
        "    riskClass: limited\n",
    )
    result = check(repo)
    got = found(result)
    assert ("use-not-allowed", "support-tickets", None, "ticket-tuner") in got
    # policies.yaml also forbids fine-tuning on PII.
    assert ("use-not-allowed", "support-tickets", "customer_email", "ticket-tuner") in got
    assert ("ai-system-added", None, None, "ticket-tuner") in got
    assert result.fails(Severity.BREAKING)


def test_policy_pack_forbids_use_by_classification(repo):
    # kb-articles allows eval, but the policy pack forbids eval on PHI.
    repo.replace("contracts/kb_articles.yaml", "classification: pii", "classification: phi")
    repo.write(
        "ai-systems.yaml",
        repo.read("ai-systems.yaml")
        + "  - id: answer-eval\n    use: eval\n    purpose: docs-search\n    inputs: [kb-articles]\n    riskClass: minimal\n",
    )
    got = found(check(repo))
    assert ("use-not-allowed", "kb-articles", "author_email", "answer-eval") in got
    assert ("use-not-allowed", "kb-articles", None, "answer-eval") not in got


def test_contract_that_feeds_an_embedder_must_be_declared(repo):
    repo.replace("ai-systems.yaml", "inputs: [support-tickets, kb-articles]", "inputs: [support-tickets]")
    result = check(repo)
    assert ("undeclared-ai-system", "kb-articles", None, None) in found(result)
    [f] = [f for f in result.findings if f.rule == "undeclared-ai-system"]
    assert f.file == "contracts/kb_articles.yaml"


def test_unknown_input_is_breaking(repo):
    repo.replace("ai-systems.yaml", "inputs: [support-tickets, kb-articles]", "inputs: [support-tickets, kb-articles, crm]")
    result = check(repo)
    assert ("ai-input-missing", None, "crm", "support-copilot") in found(result)
    [systems] = [r for r in result.results if r.status == "systems"]
    assert systems.contract_id == "ai-systems.yaml"


def test_governance_missing_and_risk_class(repo):
    text = repo.read("contracts/support_tickets.yaml")
    repo.write("contracts/support_tickets.yaml", text[: text.index("governance:")])
    repo.replace("ai-systems.yaml", "    riskClass: limited\n", "")
    result = check(repo)
    got = found(result)
    assert ("governance-missing", "support-tickets", None, None) in got
    assert ("risk-class-missing", None, None, "support-copilot") in got
    assert result.worst() == Severity.WARNING
    [f] = [f for f in result.findings if f.rule == "governance-missing"]
    assert "`customer_email`" in f.message


def test_policy_pack_can_require_governance_everywhere(repo):
    text = repo.read("contracts/kb_articles.yaml")
    repo.write("contracts/kb_articles.yaml", text[: text.index("governance:")])
    repo.replace("contracts/kb_articles.yaml", "classification: pii", "classification: internal")
    result = check(repo)
    [f] = [f for f in result.findings if f.rule == "governance-missing"]
    assert f.contract == "kb-articles" and "policies.yaml requires one" in f.message
    repo.replace("policies.yaml", "requireGovernance: true", "requireGovernance: false")
    assert not [f for f in check(repo).findings if f.rule == "governance-missing"]


def test_no_manifest_means_no_governance_rules(repo):
    repo.delete("ai-systems.yaml")
    repo.commit("drop the manifest")
    repo.write("README.txt", "unrelated\n")
    result = check(repo)
    assert result.worst() is None


def test_preexisting_governance_findings_do_not_block(repo):
    repo.replace("ai-systems.yaml", "purpose: agent-assist", "purpose: marketing")
    repo.commit("already broken on main")
    repo.write("README.txt", "unrelated\n")
    result = check(repo)
    assert not result.fails(Severity.BREAKING)
    assert all(f.preexisting for f in result.findings if f.rule == "purpose-not-allowed")


def test_invalid_manifest_and_policy_pack(repo):
    repo.replace("ai-systems.yaml", "use: rag", "use: telepathy")
    repo.write("policies.yaml", "forbiddenUses: [{classification: pii}]\n")
    result = check(repo)
    files = {f.file for f in result.findings if f.rule == "manifest-invalid"}
    assert files == {"ai-systems.yaml", "policies.yaml"}
    assert result.fails(Severity.BREAKING)


def test_duplicate_system_ids_are_invalid(repo):
    repo.write("ai-systems.yaml", "systems:\n  - {id: a, use: rag}\n  - {id: a, use: eval}\n")
    with pytest.raises(ContractError, match="duplicate AI system ids: a"):
        load_manifest(WorkingTree(repo.root), "ai-systems.yaml")


def test_contract_rejects_unknown_ai_use():
    text = "id: x\nsource: {type: pydantic, schemaFrom: m.py, model: M}\nschema: []\ngovernance: {allowedUses: [mind-reading]}\n"
    with pytest.raises(ContractError, match="allowedUses"):
        parse_contract(text)


def test_custom_manifest_path(repo):
    repo.write("governance/systems.yaml", repo.read("ai-systems.yaml").replace("agent-assist", "marketing"))
    result = run_check(WorkingTree(repo.root), "contracts", systems="governance/systems.yaml")
    assert ("purpose-not-allowed", "support-tickets") in {(f.rule, f.contract) for f in result.findings}
    assert rules(result)  # still runs the contract rules

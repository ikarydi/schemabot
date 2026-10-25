"""Run every contract through the rule engine for one pull request."""

from __future__ import annotations

from .contract import Contract, load_contracts
from .extract import TableSchema, extract
from .findings import CheckResult, ContractResult, Finding, Severity
from .rules import RULES, conformance, drift, embed_scope, evolution, policy
from .scan import QueryScan, scan_code, scan_query
from .snapshot import Snapshot


def _schema(snapshot: Snapshot, contract: Contract, warnings: list[str]) -> TableSchema | None:
    extraction = extract(snapshot, contract.source)
    warnings.extend(extraction.warnings)
    return extraction.schema


def _embed_findings(
    snapshot: Snapshot, contract: Contract, schema: TableSchema | None, warnings: list[str]
) -> list[Finding]:
    rag = contract.rag
    if rag is None or not (rag.code or rag.embed_query):
        return []
    known = set(contract.field_names) | (set(schema.columns) if schema else set())
    refs = []
    if rag.code:
        refs, scan_warnings = scan_code(snapshot, rag.code, known)
        warnings.extend(scan_warnings)
    query: QueryScan | None = None
    if rag.embed_query:
        query = scan_query(rag.embed_query, contract.source.dialect)
        if query.error:
            warnings.append(f"{contract.path}: {query.error}")
            query = None
    return embed_scope(contract, refs, query)


def _state_findings(
    snapshot: Snapshot, contract: Contract, previous: TableSchema | None, warnings: list[str]
) -> tuple[TableSchema | None, list[Finding]]:
    """Findings that describe one snapshot on its own (not the change between two)."""
    schema = _schema(snapshot, contract, warnings)
    findings = conformance(contract, schema, previous) + policy(contract)
    findings += _embed_findings(snapshot, contract, schema, warnings)
    return schema, findings


def run_check(head: Snapshot, contracts_dir: str, base: Snapshot | None = None) -> CheckResult:
    """Check every contract in ``contracts_dir`` at ``head``, against ``base`` when given.

    With a base, findings that already existed on the base branch are marked
    pre-existing: they are reported but never fail the check, so an old problem
    does not block every unrelated pull request.
    """
    result = CheckResult(base_label=base.label if base else None, head_label=head.label)
    head_contracts, errors = load_contracts(head, contracts_dir)
    base_contracts: dict[str, Contract] = {}
    if base is not None:
        loaded, _ = load_contracts(base, contracts_dir)
        base_contracts = {c.id: c for c in loaded}

    for err in errors:
        result.results.append(
            ContractResult(
                contract_id=err.path,
                contract_path=err.path,
                status="invalid",
                findings=[
                    Finding(
                        rule="contract-invalid",
                        severity=RULES["contract-invalid"].severity,
                        message=err.message,
                        file=err.path,
                    )
                ],
            )
        )

    for contract in head_contracts:
        old = base_contracts.get(contract.id)
        res = ContractResult(contract_id=contract.id, contract_path=contract.path)
        if base is None:
            res.status = "checked"
        elif old is None:
            res.status = "new"
        elif old.model_dump(by_alias=True) != contract.model_dump(by_alias=True):
            res.status = "changed"

        base_schema: TableSchema | None = None
        baseline: set[tuple] = set()
        if base is not None:
            base_warnings: list[str] = []
            ref = old or contract
            base_schema, base_findings = _state_findings(base, ref, None, base_warnings)
            baseline = {f.key() for f in base_findings}

        head_schema, findings = _state_findings(head, contract, base_schema, res.warnings)
        findings += drift(contract, head_schema, base_schema)
        if base is not None:
            findings += evolution(old, contract)
            for f in findings:
                if f.key() in baseline:
                    f.preexisting = True
        res.findings = sorted(findings, key=lambda f: (-f.severity, f.rule, f.field or ""))
        result.results.append(res)

    for cid, old in base_contracts.items():
        if any(c.id == cid for c in head_contracts):
            continue
        result.results.append(
            ContractResult(
                contract_id=cid,
                contract_path=old.path,
                status="removed",
                findings=[
                    Finding(
                        rule="contract-removed",
                        severity=RULES["contract-removed"].severity,
                        message=f"Contract `{cid}` was deleted; its source is no longer checked.",
                        contract=cid,
                        file=old.path,
                    )
                ],
            )
        )
    return result


__all__ = ["run_check", "Severity"]

"""The deterministic rule engine.

Every pass/fail decision is made here, from the contract at base and head, the
source schema at base and head, the embed-scope scan, and (for governance) the
AI systems manifest and policy pack. No LLM is involved; the same inputs always
give the same findings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .contract import Contract
from .extract import TableSchema
from .governance import AISystem, Manifest, PolicyPack
from .lineage import Lineage, ProviderUse
from .providers import region_allowed
from .findings import Finding, Severity
from .scan import EmbedRef, QueryScan
from .types import compatible

B, R, W, I = Severity.BREAKING, Severity.REINDEX, Severity.WARNING, Severity.INFO


@dataclass(frozen=True)
class Rule:
    id: str
    severity: Severity
    title: str


RULES: dict[str, Rule] = {
    r.id: r
    for r in [
        Rule("contract-invalid", B, "Contract file is invalid"),
        Rule("source-missing", B, "Contract source table or model not found"),
        Rule("filter-key-missing", B, "Retriever filter key missing from the source"),
        Rule("field-missing", B, "Contract field missing from the source"),
        Rule("type-mismatch", B, "Source type differs from the contract"),
        Rule("nullability-mismatch", B, "Required contract field is nullable in the source"),
        Rule("never-embed-violation", B, "Sensitive or never-embed field reaches the embedder"),
        Rule("pii-in-embed-scope", B, "Sensitive field listed in rag.embed"),
        Rule("filter-key-removed", B, "Filter key removed from the contract"),
        Rule("contract-type-changed", B, "Contract changed a field's type"),
        Rule("contract-guarantee-relaxed", B, "Contract made a required field optional"),
        Rule("embed-query-star", B, "rag.embedQuery uses SELECT * while fields are never-embed"),
        Rule("manifest-invalid", B, "AI systems manifest or policy pack is invalid"),
        Rule("ai-input-missing", B, "AI system lists an input that is not a contract"),
        Rule("undeclared-ai-system", B, "Contract feeds an embedder that no AI system declares"),
        Rule("purpose-not-allowed", B, "AI system uses data for a purpose the contract does not allow"),
        Rule("use-not-allowed", B, "AI system uses data in a way the contract or policy forbids"),
        Rule("provider-not-allowed", B, "Data reaches a model provider the contract or policy does not allow"),
        Rule("residency-violation", B, "Data is processed in a region outside the contract's residency"),
        Rule("undeclared-training", B, "Code starts a fine-tuning job that no fine-tuning AI system declares"),
        Rule("index-changed", R, "Embedding model, dimension or chunking changed"),
        Rule("embed-scope-changed", R, "Set of embedded fields changed"),
        Rule("embedded-type-changed", R, "Type of an embedded field changed"),
        Rule("embed-scope-exceeded", W, "Code embeds a field outside rag.embed"),
        Rule("never-embed-relaxed", W, "Field removed from rag.neverEmbed"),
        Rule("contract-field-removed", W, "Field removed from the contract"),
        Rule("contract-removed", W, "Contract deleted"),
        Rule("column-removed", W, "Undeclared column removed from the source"),
        Rule("column-type-changed", W, "Undeclared column changed type"),
        Rule("possible-pii", W, "New column looks like personal data but is not classified"),
        Rule("no-filter-keys", W, "RAG contract declares no filter keys"),
        Rule("embed-scope-undeclared", W, "RAG contract does not declare rag.embed"),
        Rule("governance-missing", W, "Contract feeds an AI system but has no governance block"),
        Rule("risk-class-missing", W, "AI system declares no risk class"),
        Rule("provider-undeclared", W, "Code calls a model provider the AI system does not declare"),
        Rule("region-undeclared", W, "Provider region unknown, so residency cannot be checked"),
        Rule("provider-changed", W, "AI system changed its model or embedding providers"),
        Rule("field-undeclared", I, "New source column not in the contract"),
        Rule("contract-added", I, "New contract"),
        Rule("ai-system-added", I, "New AI system"),
    ]
}

_PII_PATTERNS = re.compile(
    r"(e_?mail|phone|mobile|ssn|social_?security|passport|birth|dob\b|address|street|"
    r"zip|postcode|postal|first_?name|last_?name|full_?name|surname|ip_?addr|"
    r"credit_?card|card_?number|iban|account_?number|tax_?id|national_?id|"
    r"password|secret|token|api_?key|salary|gender|ethnic|religion|health|diagnos)",
    re.IGNORECASE,
)


def looks_like_pii(name: str) -> bool:
    return bool(_PII_PATTERNS.search(name))


def _f(rule: str, message: str, contract: Contract, **kw) -> Finding:
    return Finding(rule=rule, severity=RULES[rule].severity, message=message, contract=contract.id, **kw)


def _origin(schema: TableSchema | None, contract: Contract) -> str:
    return (schema.origin if schema and schema.origin else None) or contract.path


def conformance(contract: Contract, head: TableSchema | None, base: TableSchema | None) -> list[Finding]:
    """Does the source, as of this PR, still satisfy the contract?"""
    if head is None:
        where = contract.source.schema_from
        return [
            _f(
                "source-missing",
                f"{contract.source.type} source `{contract.source.target}` was not found in `{where}`.",
                contract,
                file=contract.path,
                hint="Check source.schemaFrom and source.table/model, or restore the dropped table.",
            )
        ]
    out: list[Finding] = []
    filter_keys = set(contract.rag.filter_keys) if contract.rag else set()
    origin = _origin(head, contract)
    for field in contract.fields:
        col = head.column(field.name)
        if col is None:
            renamed = None
            if base is not None and base.column(field.name) is not None:
                added = [c for c in head.columns.values() if base.column(c.name) is None]
                same_type = [c for c in added if c.type == base.columns[field.name].type]
                renamed = same_type[0].name if len(same_type) == 1 else None
            hint = f"Looks like a rename to `{renamed}`; update the contract and every consumer together." if renamed else None
            if field.name in filter_keys:
                out.append(
                    _f(
                        "filter-key-missing",
                        f"`{field.name}` is a retriever filter key but no longer exists in "
                        f"`{head.name}`. Retrieval would stop filtering by it and could return "
                        f"another tenant's documents.",
                        contract,
                        field=field.name,
                        file=origin,
                        hint=hint or "Keep the column, or move the filter to a new key in the same PR.",
                    )
                )
            else:
                out.append(
                    _f(
                        "field-missing",
                        f"`{field.name}` is in the contract but not in `{head.name}`.",
                        contract,
                        field=field.name,
                        file=origin,
                        hint=hint or "Restore the column, or remove it from the contract with a deprecation note.",
                    )
                )
            continue
        if not compatible(field.normalized_type, col.type):
            out.append(
                _f(
                    "type-mismatch",
                    f"`{field.name}` is `{field.type}` in the contract but `{col.raw_type}` ({col.type}) in `{head.name}`.",
                    contract,
                    field=field.name,
                    file=origin,
                )
            )
        if field.required and col.nullable:
            out.append(
                _f(
                    "nullability-mismatch",
                    f"`{field.name}` is required in the contract but nullable in `{head.name}`.",
                    contract,
                    field=field.name,
                    file=origin,
                    hint="Add NOT NULL (with a backfill) or mark the field `required: false`.",
                )
            )
    declared = set(contract.field_names)
    noun = "Column" if base is None else "New column"
    for col in head.columns.values():
        if col.name in declared:
            continue
        if base is not None and base.column(col.name) is not None:
            continue
        if looks_like_pii(col.name):
            out.append(
                _f(
                    "possible-pii",
                    f"{noun} `{col.name}` looks like personal data and is not classified in the contract.",
                    contract,
                    field=col.name,
                    file=origin,
                    hint="Declare it with `classification: pii` so SchemaBot keeps it out of embeddings.",
                )
            )
        else:
            out.append(
                _f(
                    "field-undeclared",
                    f"{noun} `{col.name}` ({col.raw_type or col.type}) is not in the contract.",
                    contract,
                    field=col.name,
                    file=origin,
                )
            )
    return out


def drift(contract: Contract, head: TableSchema | None, base: TableSchema | None) -> list[Finding]:
    """Changes to columns the contract does not declare (declared ones are covered by conformance)."""
    if head is None or base is None:
        return []
    out: list[Finding] = []
    declared = set(contract.field_names)
    origin = _origin(head, contract)
    for name, old in base.columns.items():
        if name in declared:
            continue
        new = head.column(name)
        if new is None:
            out.append(
                _f(
                    "column-removed",
                    f"Column `{name}` was removed from `{head.name}`. It is not in the contract, "
                    f"but undeclared consumers may read it.",
                    contract,
                    field=name,
                    file=origin,
                )
            )
        elif new.type != old.type:
            out.append(
                _f(
                    "column-type-changed",
                    f"Column `{name}` changed from `{old.raw_type}` to `{new.raw_type}`.",
                    contract,
                    field=name,
                    file=origin,
                )
            )
    embedded = set(contract.rag.embed) if contract.rag else set()
    for name in sorted(embedded):
        old, new = base.column(name), head.column(name)
        if old and new and old.type != new.type:
            out.append(
                _f(
                    "embedded-type-changed",
                    f"Embedded field `{name}` changed from `{old.raw_type}` to `{new.raw_type}`; "
                    f"existing vectors may no longer match how the text is built.",
                    contract,
                    field=name,
                    file=origin,
                )
            )
    return out


def evolution(base: Contract | None, head: Contract) -> list[Finding]:
    """What changed in the contract itself between base and head."""
    if base is None:
        return [_f("contract-added", f"New contract `{head.id}`.", head, file=head.path)]
    out: list[Finding] = []
    for old in base.fields:
        new = head.field(old.name)
        if new is None:
            out.append(
                _f(
                    "contract-field-removed",
                    f"`{old.name}` was removed from the contract; consumers relying on it lose their guarantee.",
                    head,
                    field=old.name,
                    file=head.path,
                )
            )
            continue
        if old.normalized_type != new.normalized_type and "unknown" not in (old.normalized_type, new.normalized_type):
            out.append(
                _f(
                    "contract-type-changed",
                    f"`{old.name}` changed type in the contract from `{old.type}` to `{new.type}`.",
                    head,
                    field=old.name,
                    file=head.path,
                    hint="Type changes break consumers; add a new field and deprecate the old one instead.",
                )
            )
        if old.required and not new.required:
            out.append(
                _f(
                    "contract-guarantee-relaxed",
                    f"`{old.name}` is no longer required; consumers may now receive nulls.",
                    head,
                    field=old.name,
                    file=head.path,
                )
            )
    old_rag, new_rag = base.rag, head.rag
    old_keys = set(old_rag.filter_keys) if old_rag else set()
    new_keys = set(new_rag.filter_keys) if new_rag else set()
    for key in sorted(old_keys - new_keys):
        out.append(
            _f(
                "filter-key-removed",
                f"`{key}` is no longer a filter key. Retrieval that relied on it for tenant or "
                f"access isolation would stop filtering.",
                head,
                field=key,
                file=head.path,
            )
        )
    old_never = base.never_embed()
    new_never = head.never_embed()
    for name in sorted(old_never - new_never):
        if head.field(name) is not None:
            out.append(
                _f(
                    "never-embed-relaxed",
                    f"`{name}` is no longer protected from embedding (removed from neverEmbed or declassified).",
                    head,
                    field=name,
                    file=head.path,
                )
            )
    old_embed = set(old_rag.embed) if old_rag else set()
    new_embed = set(new_rag.embed) if new_rag else set()
    if old_embed != new_embed:
        added = ", ".join(f"`{n}`" for n in sorted(new_embed - old_embed)) or "none"
        removed = ", ".join(f"`{n}`" for n in sorted(old_embed - new_embed)) or "none"
        out.append(
            _f(
                "embed-scope-changed",
                f"Embedded fields changed (added: {added}; removed: {removed}). Existing vectors were built "
                f"from different text.",
                head,
                file=head.path,
                hint="Re-embed the corpus, or version the index and backfill.",
            )
        )
    old_index = old_rag.index.model_dump(exclude_none=True) if old_rag and old_rag.index else {}
    new_index = new_rag.index.model_dump(exclude_none=True) if new_rag and new_rag.index else {}
    if old_index != new_index:
        changes = []
        for key in sorted(set(old_index) | set(new_index)):
            if old_index.get(key) != new_index.get(key):
                changes.append(f"{key}: {old_index.get(key)!r} → {new_index.get(key)!r}")
        out.append(
            _f(
                "index-changed",
                "Index settings changed (" + "; ".join(changes) + "). Vectors built with the old settings "
                "are not comparable with new ones.",
                head,
                file=head.path,
                hint="Plan a full re-index (or a dual-write to a new index) before merging.",
            )
        )
    return out


def policy(contract: Contract) -> list[Finding]:
    """Checks on the head contract alone."""
    rag = contract.rag
    if rag is None:
        return []
    out: list[Finding] = []
    for name in rag.embed:
        field = contract.field(name)
        if field is not None and field.sensitive:
            out.append(
                _f(
                    "pii-in-embed-scope",
                    f"`{name}` is classified `{field.classification}` but listed in rag.embed.",
                    contract,
                    field=name,
                    file=contract.path,
                    hint="Remove it from rag.embed, or embed a redacted derivative instead.",
                )
            )
    if not rag.filter_keys:
        out.append(
            _f(
                "no-filter-keys",
                "No rag.filterKeys declared. If this index serves more than one tenant or access level, "
                "retrieval has nothing to filter on.",
                contract,
                file=contract.path,
            )
        )
    if not rag.embed:
        out.append(
            _f(
                "embed-scope-undeclared",
                "rag.embed is empty, so SchemaBot cannot tell intended embedding from accidental embedding.",
                contract,
                file=contract.path,
            )
        )
    return out


def embed_scope(
    contract: Contract, refs: list[EmbedRef], query: QueryScan | None
) -> list[Finding]:
    """Compare what the code (or the declared query) embeds with what the contract allows."""
    rag = contract.rag
    if rag is None:
        return []
    out: list[Finding] = []
    never = contract.never_embed()
    allowed = set(rag.embed)
    reported: set[tuple[str, str, int]] = set()
    for ref in refs:
        if ref.field in never:
            out.append(
                _f(
                    "never-embed-violation",
                    f"`{ref.field}` reaches the embedder via `{ref.sink}`, but the contract says it must never be embedded.",
                    contract,
                    field=ref.field,
                    file=ref.file,
                    line=ref.line,
                    hint="Drop the field from the text you embed, or store it as filterable metadata instead.",
                )
            )
        elif allowed and ref.field not in allowed and ref.field not in rag.filter_keys:
            key = (ref.field, ref.file, ref.line)
            if key in reported:
                continue
            reported.add(key)
            out.append(
                _f(
                    "embed-scope-exceeded",
                    f"`{ref.field}` reaches the embedder via `{ref.sink}` but is not in rag.embed.",
                    contract,
                    field=ref.field,
                    file=ref.file,
                    line=ref.line,
                    hint="Add it to rag.embed if intended (this triggers a re-index), or stop embedding it.",
                )
            )
    if query is not None:
        if query.star and never:
            out.append(
                _f(
                    "embed-query-star",
                    "rag.embedQuery selects `*`, so never-embed fields ("
                    + ", ".join(f"`{n}`" for n in sorted(never))
                    + ") reach the embedder.",
                    contract,
                    file=contract.path,
                )
            )
        for name in sorted(query.fields & never):
            out.append(
                _f(
                    "never-embed-violation",
                    f"`{name}` is selected by rag.embedQuery, but the contract says it must never be embedded.",
                    contract,
                    field=name,
                    file=contract.path,
                )
            )
    return out


def _g(rule: str, message: str, contract: str | None, system: AISystem | str | None, file: str, **kw) -> Finding:
    return Finding(
        rule=rule,
        severity=RULES[rule].severity,
        message=message,
        contract=contract,
        system=system.id if isinstance(system, AISystem) else system,
        file=file,
        **kw,
    )


def _feeds_embedder(contract: Contract) -> bool:
    return contract.rag is not None and bool(contract.rag.code or contract.rag.embed_query)


def governance(contracts: list[Contract], manifest: Manifest | None, policies: PolicyPack | None) -> list[Finding]:
    """Is every AI system declared, and does it use each input only as allowed?

    Runs only when the repository has an AI systems manifest, so adopting the
    governance rules is a deliberate step.
    """
    if manifest is None:
        return []
    out: list[Finding] = []
    by_id = {c.id: c for c in contracts}
    forbidden = policies.forbidden_uses if policies else []
    fed: set[str] = set()
    for system in manifest.systems:
        if system.risk_class is None:
            out.append(
                _g(
                    "risk-class-missing",
                    f"AI system `{system.id}` declares no `riskClass`.",
                    None,
                    system,
                    manifest.path,
                    hint="Declare its EU AI Act tier (minimal, limited, high) so the inventory and evidence can show it.",
                )
            )
        for cid in system.inputs:
            contract = by_id.get(cid)
            if contract is None:
                out.append(
                    _g(
                        "ai-input-missing",
                        f"AI system `{system.id}` lists input `{cid}`, but no contract has that id.",
                        None,
                        system,
                        manifest.path,
                        field=cid,
                        hint="Fix the id, or add a contract for the source so its use can be checked.",
                    )
                )
                continue
            fed.add(cid)
            gov = contract.governance
            if gov and gov.purposes and system.purpose not in gov.purposes:
                used = f"for `{system.purpose}`" if system.purpose else "without declaring a purpose"
                allowed = ", ".join(f"`{p}`" for p in gov.purposes)
                out.append(
                    _g(
                        "purpose-not-allowed",
                        f"AI system `{system.id}` uses `{cid}` {used}, but the contract allows only {allowed}.",
                        cid,
                        system,
                        manifest.path,
                        hint="Change the system's purpose, or have the data owner add the purpose to the contract.",
                    )
                )
            if gov and gov.allowed_uses and system.use not in gov.allowed_uses:
                allowed = ", ".join(f"`{u}`" for u in gov.allowed_uses)
                out.append(
                    _g(
                        "use-not-allowed",
                        f"AI system `{system.id}` uses `{cid}` for `{system.use}`, but the contract allows only {allowed}.",
                        cid,
                        system,
                        manifest.path,
                    )
                )
            for rule in forbidden:
                if system.use not in rule.uses:
                    continue
                for field in contract.fields:
                    if (field.classification or "").lower() == rule.classification.lower():
                        out.append(
                            _g(
                                "use-not-allowed",
                                f"AI system `{system.id}` uses `{cid}` for `{system.use}`, but `{field.name}` is "
                                f"classified `{field.classification}` and {policies.path} forbids that use.",
                                cid,
                                system,
                                manifest.path,
                                field=field.name,
                                hint="Leave the field out of this system's input, or use a de-identified copy.",
                            )
                        )
    require = bool(policies and policies.require_governance)
    for contract in contracts:
        if _feeds_embedder(contract) and contract.id not in fed:
            out.append(
                _g(
                    "undeclared-ai-system",
                    f"`{contract.id}` feeds an embedder, but no AI system in {manifest.path} lists it as an input.",
                    contract.id,
                    None,
                    contract.path,
                    hint="Add the system that uses it to the manifest, with its purpose and providers.",
                )
            )
        if contract.governance is None and (contract.id in fed or _feeds_embedder(contract)):
            sensitive = [f.name for f in contract.fields if f.sensitive]
            if sensitive or require:
                why = (
                    "it holds " + ", ".join(f"`{n}`" for n in sensitive)
                    if sensitive
                    else f"{policies.path} requires one"  # type: ignore[union-attr]
                )
                out.append(
                    _g(
                        "governance-missing",
                        f"`{contract.id}` feeds an AI system but has no governance block, and {why}.",
                        contract.id,
                        None,
                        contract.path,
                        hint="Add `governance:` with its purposes and allowed uses.",
                    )
                )
    return out


def _describe(p: ProviderUse) -> str:
    details = ", ".join(x for x in (p.role, p.model, p.region and f"region {p.region}") if x)
    return f"`{p.name}`" + (f" ({details})" if details else "")


def _where(p: ProviderUse) -> dict:
    return {"file": p.file or "", "line": p.line}


def providers(lineage: Lineage | None, contracts: list[Contract], policies: PolicyPack | None) -> list[Finding]:
    """Which providers and regions each AI system's inputs reach, against what the contracts and policies allow."""
    if lineage is None:
        return []
    out: list[Finding] = []
    by_id = {c.id: c for c in contracts}
    allow_rules = policies.allowed_providers if policies else []
    for system in lineage.systems:
        for p in system.providers:
            if not p.declared and not p.matches_declared:
                out.append(
                    _g(
                        "provider-undeclared",
                        f"`{p.via}` sends data from AI system `{system.id}` to {_describe(p)}, which "
                        f"{lineage.manifest_path} does not list among its providers.",
                        None,
                        system.id,
                        provider=p.name,
                        hint="Declare the provider, its role and its region in the manifest.",
                        **_where(p),
                    )
                )
        # One entry per provider name, preferring the declared one for locations.
        names: dict[str, ProviderUse] = {}
        for p in sorted(system.providers, key=lambda p: not p.declared):
            names.setdefault(p.name, p)
        for inp in system.inputs:
            contract = by_id[inp.contract]
            gov = contract.governance
            for name, p in names.items():
                if gov and gov.allowed_providers and name not in {a.lower() for a in gov.allowed_providers}:
                    allowed = ", ".join(f"`{a}`" for a in gov.allowed_providers)
                    out.append(
                        _g(
                            "provider-not-allowed",
                            f"AI system `{system.id}` sends `{contract.id}` to `{name}`, but the contract allows only {allowed}.",
                            contract.id,
                            system.id,
                            provider=name,
                            **_where(p),
                        )
                    )
                for rule in allow_rules:
                    if name in {a.lower() for a in rule.providers}:
                        continue
                    for f in contract.fields:
                        if (f.classification or "").lower() != rule.classification.lower():
                            continue
                        out.append(
                            _g(
                                "provider-not-allowed",
                                f"AI system `{system.id}` sends `{contract.id}` to `{name}`, but `{f.name}` is classified "
                                f"`{f.classification}` and {policies.path} allows only "  # type: ignore[union-attr]
                                + ", ".join(f"`{a}`" for a in rule.providers)
                                + ".",
                                contract.id,
                                system.id,
                                field=f.name,
                                provider=name,
                                hint="Use an allowed provider, or keep the field out of this system's input.",
                                **_where(p),
                            )
                        )
            if not (gov and gov.residency):
                continue
            zones = ", ".join(f"`{r}`" for r in gov.residency)
            for p in system.providers:
                if p.name == "local" or (not p.declared and p.matches_declared and not p.region):
                    continue
                if p.region is None:
                    out.append(
                        _g(
                            "region-undeclared",
                            f"`{contract.id}` must stay in {zones}, but AI system `{system.id}` declares no region "
                            f"for {_describe(p)}.",
                            contract.id,
                            system.id,
                            provider=p.name,
                            hint="Add `region:` to the provider in the manifest, or pass it in code.",
                            **_where(p),
                        )
                    )
                elif not region_allowed(p.region, gov.residency):
                    out.append(
                        _g(
                            "residency-violation",
                            f"`{contract.id}` must stay in {zones}, but AI system `{system.id}` sends it to "
                            f"{_describe(p)}.",
                            contract.id,
                            system.id,
                            provider=p.name,
                            hint="Use a region inside the contract's residency, or have the data owner change it.",
                            **_where(p),
                        )
                    )
    finetune_files = {f for s in lineage.systems if s.use == "finetune" for f in s.files}
    for t in lineage.training:
        if t.file in finetune_files:
            continue
        owners = lineage.contract_files.get(t.file, [])
        out.append(
            _g(
                "undeclared-training",
                f"`{t.via}` starts a fine-tuning job, but no AI system with `use: finetune` covers `{t.file}`.",
                owners[0] if owners else None,
                None,
                t.file,
                line=t.line,
                hint="Declare the fine-tuning system and its inputs, so the contracts' allowed uses are checked.",
            )
        )
    return out


def systems_evolution(base: Manifest | None, head: Manifest | None) -> list[Finding]:
    """What changed in the manifest between base and head."""
    if head is None:
        return []
    old = {s.id: s for s in base.systems} if base else {}
    out: list[Finding] = []
    for s in head.systems:
        before = old.get(s.id)
        if before is None:
            out.append(_g("ai-system-added", f"New AI system `{s.id}` ({s.use}).", None, s, head.path))
            continue
        as_set = lambda sys: {(p.name.lower(), p.role, p.model, p.region) for p in sys.providers}  # noqa: E731
        if as_set(before) != as_set(s):
            fmt = lambda items: ", ".join(  # noqa: E731
                "/".join(x for x in item if x) for item in sorted(items, key=str)
            ) or "none"
            out.append(
                _g(
                    "provider-changed",
                    f"AI system `{s.id}` changed providers (added: {fmt(as_set(s) - as_set(before))}; "
                    f"removed: {fmt(as_set(before) - as_set(s))}).",
                    None,
                    s,
                    head.path,
                    hint="A new embedding provider or model needs a re-index; a new provider may need a DPA review.",
                )
            )
    return out

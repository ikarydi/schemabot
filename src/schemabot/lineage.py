"""The lineage graph: source, then fields, then pipeline code, then AI system, then provider.

Built from the contracts, the AI systems manifest and a scan of the pipeline
code on every run, and never stored. The provider rules run over it and
``schemabot lineage`` prints it.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from .contract import Contract
from .governance import Manifest
from .providers import TrainingRef, scan_providers
from .snapshot import Snapshot


@dataclass
class ProviderUse:
    name: str
    role: str | None = None
    model: str | None = None
    region: str | None = None
    endpoint: str | None = None
    # Where it comes from: the manifest (``declared``) or a call in the code.
    declared: bool = False
    file: str | None = None
    line: int | None = None
    via: str | None = None
    # A call in the code to a provider the manifest also declares.
    matches_declared: bool = False


@dataclass
class InputLineage:
    contract: str
    source: str
    embedded: list[str] = field(default_factory=list)
    sensitive: list[str] = field(default_factory=list)


@dataclass
class SystemLineage:
    id: str
    use: str
    purpose: str | None
    risk_class: str | None
    inputs: list[InputLineage] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    providers: list[ProviderUse] = field(default_factory=list)
    training: list[TrainingRef] = field(default_factory=list)


@dataclass
class Lineage:
    systems: list[SystemLineage] = field(default_factory=list)
    # Fine-tuning calls in pipeline code, by file, whichever system the file belongs to.
    training: list[TrainingRef] = field(default_factory=list)
    # File -> contracts whose rag.code covers it.
    contract_files: dict[str, list[str]] = field(default_factory=dict)
    manifest_path: str = ""
    warnings: list[str] = field(default_factory=list)

    def system(self, system_id: str) -> SystemLineage | None:
        return next((s for s in self.systems if s.id == system_id), None)

    def to_dict(self) -> dict:
        return {
            "manifest": self.manifest_path,
            "systems": [asdict(s) for s in self.systems],
            "training": [asdict(t) for t in self.training],
        }


def _files(snapshot: Snapshot, paths: list[str]) -> list[str]:
    return sorted({f for p in paths for f in snapshot.list_files(p) if f.endswith(".py")})


def build_lineage(snapshot: Snapshot, contracts: list[Contract], manifest: Manifest) -> Lineage:
    by_id = {c.id: c for c in contracts}
    lineage = Lineage(manifest_path=manifest.path)
    for contract in contracts:
        if contract.rag:
            for f in _files(snapshot, contract.rag.code):
                lineage.contract_files.setdefault(f, []).append(contract.id)

    all_paths = [p for s in manifest.systems for p in s.code] + [
        p for c in contracts if c.rag for p in c.rag.code
    ]
    refs, training, warnings = scan_providers(snapshot, all_paths)
    lineage.training = training
    lineage.warnings = warnings

    for system in manifest.systems:
        inputs = [by_id[cid] for cid in system.inputs if cid in by_id]
        paths = list(system.code) + [p for c in inputs if c.rag for p in c.rag.code]
        files = _files(snapshot, paths)
        node = SystemLineage(
            id=system.id,
            use=system.use,
            purpose=system.purpose,
            risk_class=system.risk_class,
            files=files,
            inputs=[
                InputLineage(
                    contract=c.id,
                    source=f"{c.source.type}:{c.source.target}",
                    embedded=list(c.rag.embed) if c.rag else [],
                    sensitive=[f.name for f in c.fields if f.sensitive],
                )
                for c in inputs
            ],
            training=[t for t in training if t.file in files],
        )
        declared = {p.name.lower() for p in system.providers}
        for p in system.providers:
            node.providers.append(
                ProviderUse(name=p.name.lower(), role=p.role, model=p.model, region=p.region, declared=True, file=manifest.path)
            )
        for ref in refs:
            if ref.file not in files:
                continue
            node.providers.append(
                ProviderUse(
                    name=ref.provider,
                    model=ref.model,
                    region=ref.region,
                    endpoint=ref.endpoint,
                    file=ref.file,
                    line=ref.line,
                    via=ref.via,
                    matches_declared=ref.provider in declared,
                )
            )
        lineage.systems.append(node)
    return lineage


def to_text(lineage: Lineage) -> str:
    out: list[str] = []
    for s in lineage.systems:
        out.append(f"{s.id} ({s.use}{', ' + s.purpose if s.purpose else ''}{', risk ' + s.risk_class if s.risk_class else ''})")
        for i in s.inputs:
            line = f"  <- {i.contract} [{i.source}]"
            if i.embedded:
                line += f" embeds {', '.join(i.embedded)}"
            if i.sensitive:
                line += f"; sensitive: {', '.join(i.sensitive)}"
            out.append(line)
        for f in s.files:
            out.append(f"  code {f}")
        for p in s.providers:
            where = "manifest" if p.declared else f"{p.file}:{p.line}"
            details = ", ".join(x for x in (p.role, p.model, p.region and f"region {p.region}") if x)
            out.append(f"  -> {p.name}{' (' + details + ')' if details else ''} [{where}]")
        for t in s.training:
            out.append(f"  trains via {t.via} [{t.file}:{t.line}]")
    return "\n".join(out) + "\n" if out else "No AI systems declared.\n"


def _mid(text: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in text)


def to_mermaid(lineage: Lineage) -> str:
    """A Mermaid flowchart: contracts on the left, systems in the middle, providers on the right."""
    lines = ["flowchart LR"]
    edges: set[str] = set()
    for s in lineage.systems:
        sid = f"sys_{_mid(s.id)}"
        lines.append(f'  {sid}["{s.id}<br/>{s.use}"]')
        for i in s.inputs:
            cid = f"c_{_mid(i.contract)}"
            lines.append(f'  {cid}[("{i.contract}")]')
            edges.add(f"  {cid} --> {sid}")
        for p in s.providers:
            if not p.declared and p.matches_declared:
                continue
            pid = f"p_{_mid(p.name)}_{_mid(p.region or 'any')}"
            label = p.name + (f"<br/>{p.region}" if p.region else "")
            lines.append(f'  {pid}{{{{"{label}"}}}}')
            edges.add(f"  {sid} --> {pid}")
    return "\n".join(dict.fromkeys(lines)) + "\n" + "\n".join(sorted(edges)) + "\n"


def to_json(lineage: Lineage) -> str:
    return json.dumps(lineage.to_dict(), indent=2) + "\n"

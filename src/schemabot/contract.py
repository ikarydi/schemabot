"""The data contract model and loader.

A contract is a YAML file, one per source a pipeline reads. The format is a
subset of the Open Data Contract Standard (ODCS v3) with a ``rag`` block that
says what may be embedded, what never may, and which keys the retriever must
filter on, and an optional ``governance`` block that says what the data may be
used for by AI systems.
"""

from __future__ import annotations

from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .snapshot import Snapshot
from .types import normalize_type

SENSITIVE_CLASSES = {"pii", "secret", "restricted", "sensitive", "phi", "pci"}

# How an AI system uses data: retrieval, fine-tuning, evaluation, or prompt context.
AIUse = Literal["rag", "finetune", "eval", "prompt"]


class ContractField(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str
    type: str = "unknown"
    required: bool = False
    primary_key: bool = Field(False, alias="primaryKey")
    classification: str | None = None
    description: str | None = None

    @property
    def normalized_type(self) -> str:
        return normalize_type(self.type)

    @property
    def sensitive(self) -> bool:
        return (self.classification or "").lower() in SENSITIVE_CLASSES


class Source(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    type: Literal["postgres", "sql", "pydantic"]
    schema_from: str = Field(alias="schemaFrom")
    table: str | None = None
    model: str | None = None
    dialect: str = "postgres"

    @model_validator(mode="after")
    def _target(self) -> "Source":
        if self.type in ("postgres", "sql") and not self.table:
            raise ValueError("source.table is required for SQL sources")
        if self.type == "pydantic" and not self.model:
            raise ValueError("source.model is required for pydantic sources")
        return self

    @property
    def target(self) -> str:
        return self.table if self.type in ("postgres", "sql") else self.model  # type: ignore[return-value]


class Chunking(BaseModel):
    model_config = ConfigDict(extra="allow")

    strategy: str | None = None
    size: int | None = None
    overlap: int | None = None


class IndexSpec(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str | None = None
    dimension: int | None = None
    chunking: Chunking | None = None


class RagSpec(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    embed: list[str] = Field(default_factory=list)
    never_embed: list[str] = Field(default_factory=list, alias="neverEmbed")
    filter_keys: list[str] = Field(default_factory=list, alias="filterKeys")
    index: IndexSpec | None = None
    code: list[str] = Field(default_factory=list)
    embed_query: str | None = Field(None, alias="embedQuery")


class Retention(BaseModel):
    model_config = ConfigDict(extra="allow")

    days: int | None = Field(None, ge=1)
    erasure: bool = False


class Governance(BaseModel):
    """What AI systems may do with this data. Every key is optional."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    owner: str | None = None
    approvers: list[str] = Field(default_factory=list)
    purposes: list[str] = Field(default_factory=list)
    legal_basis: str | None = Field(None, alias="legalBasis")
    residency: list[str] = Field(default_factory=list)
    retention: Retention | None = None
    deletion: str | None = None
    allowed_uses: list[AIUse] = Field(default_factory=list, alias="allowedUses")
    allowed_providers: list[str] = Field(default_factory=list, alias="allowedProviders")


class Contract(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    api_version: str = Field("v3.0.0", alias="apiVersion")
    kind: Literal["DataContract"] = "DataContract"
    id: str
    owner: str | None = None
    source: Source
    fields: list[ContractField] = Field(alias="schema")
    rag: RagSpec | None = None
    governance: Governance | None = None

    # Set by the loader, not by the YAML.
    path: str = Field("", exclude=True)

    @model_validator(mode="after")
    def _consistent(self) -> "Contract":
        names = [f.name for f in self.fields]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate fields in schema: {', '.join(dupes)}")
        if self.rag:
            known = set(names)
            for label, values in (
                ("rag.embed", self.rag.embed),
                ("rag.neverEmbed", self.rag.never_embed),
                ("rag.filterKeys", self.rag.filter_keys),
            ):
                unknown = [v for v in values if v not in known]
                if unknown:
                    raise ValueError(f"{label} names fields not in schema: {', '.join(unknown)}")
            both = sorted(set(self.rag.embed) & set(self.rag.never_embed))
            if both:
                raise ValueError(f"fields are in both rag.embed and rag.neverEmbed: {', '.join(both)}")
        return self

    def field(self, name: str) -> ContractField | None:
        return next((f for f in self.fields if f.name == name), None)

    @property
    def field_names(self) -> list[str]:
        return [f.name for f in self.fields]

    def never_embed(self) -> set[str]:
        """Fields that must never reach an embedder: declared, or classified sensitive."""
        declared = set(self.rag.never_embed) if self.rag else set()
        return declared | {f.name for f in self.fields if f.sensitive}


class ContractError(Exception):
    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path
        self.message = message


def format_validation(err: ValidationError) -> str:
    parts = []
    for e in err.errors():
        loc = ".".join(str(p) for p in e["loc"])
        msg = e["msg"].removeprefix("Value error, ")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts)


def parse_contract(text: str, path: str = "<contract>") -> Contract:
    try:
        data: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ContractError(path, f"invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ContractError(path, "expected a YAML mapping at the top level")
    try:
        contract = Contract.model_validate(data)
    except ValidationError as exc:
        raise ContractError(path, format_validation(exc)) from exc
    contract.path = path
    return contract


def load_contracts(snapshot: Snapshot, directory: str) -> tuple[list[Contract], list[ContractError]]:
    """Load every ``*.yaml``/``*.yml`` contract under ``directory``.

    Files that fail to parse are returned as errors rather than raised, so one
    bad contract does not hide findings in the others.
    """
    contracts: list[Contract] = []
    errors: list[ContractError] = []
    seen: dict[str, str] = {}
    for path in snapshot.list_files(directory):
        if not path.endswith((".yaml", ".yml")):
            continue
        text = snapshot.read_text(path)
        if text is None:
            continue
        try:
            contract = parse_contract(text, path)
        except ContractError as exc:
            errors.append(exc)
            continue
        if contract.id in seen:
            errors.append(ContractError(path, f"duplicate contract id {contract.id!r} (also in {seen[contract.id]})"))
            continue
        seen[contract.id] = path
        contracts.append(contract)
    return contracts, errors

"""The AI systems manifest and the org policy pack.

``ai-systems.yaml`` lists every AI system the repository builds (a RAG index, a
fine-tuning or eval dataset, a prompt-context loader), what it is for, which
contracts feed it and which providers it calls. ``policies.yaml`` holds rules
that apply to every contract. Both are optional: without a manifest, the
governance rules do not run and SchemaBot behaves as before.
"""

from __future__ import annotations

from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .contract import AIUse, ContractError, format_validation
from .snapshot import Snapshot

# EU AI Act tiers, as declared by the team that owns the system.
RiskClass = Literal["minimal", "limited", "high", "unacceptable"]


class Provider(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    role: str | None = None  # embedding | generation | training | ...
    model: str | None = None
    region: str | None = None


class AISystem(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    use: AIUse
    purpose: str | None = None
    owner: str | None = None
    inputs: list[str] = Field(default_factory=list)
    code: list[str] = Field(default_factory=list)
    providers: list[Provider] = Field(default_factory=list)
    risk_class: RiskClass | None = Field(None, alias="riskClass")


class Manifest(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    api_version: str = Field("v1", alias="apiVersion")
    kind: Literal["AISystems"] = "AISystems"
    systems: list[AISystem] = Field(default_factory=list)

    path: str = Field("", exclude=True)

    @model_validator(mode="after")
    def _unique(self) -> "Manifest":
        ids = [s.id for s in self.systems]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate AI system ids: {', '.join(dupes)}")
        return self

    def system(self, system_id: str) -> AISystem | None:
        return next((s for s in self.systems if s.id == system_id), None)


class ForbiddenUse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    classification: str
    uses: list[AIUse]


class AllowedProviders(BaseModel):
    model_config = ConfigDict(extra="forbid")

    classification: str
    providers: list[str]


class PolicyPack(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    api_version: str = Field("v1", alias="apiVersion")
    kind: Literal["PolicyPack"] = "PolicyPack"
    # Every contract that feeds an AI system needs a governance block, not only sensitive ones.
    require_governance: bool = Field(False, alias="requireGovernance")
    forbidden_uses: list[ForbiddenUse] = Field(default_factory=list, alias="forbiddenUses")
    # Fields with this classification may only reach these providers.
    allowed_providers: list[AllowedProviders] = Field(default_factory=list, alias="allowedProviders")

    path: str = Field("", exclude=True)


def _load(snapshot: Snapshot, path: str, model: type[BaseModel]) -> Any:
    """Parse ``path`` into ``model``; None when the file does not exist."""
    text = snapshot.read_text(path)
    if text is None:
        return None
    try:
        data: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ContractError(path, f"invalid YAML: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ContractError(path, "expected a YAML mapping at the top level")
    try:
        obj = model.model_validate(data)
    except ValidationError as exc:
        raise ContractError(path, format_validation(exc)) from exc
    obj.path = path
    return obj


def load_manifest(snapshot: Snapshot, path: str) -> Manifest | None:
    return _load(snapshot, path, Manifest)


def load_policies(snapshot: Snapshot, path: str) -> PolicyPack | None:
    return _load(snapshot, path, PolicyPack)

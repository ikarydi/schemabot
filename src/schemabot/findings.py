from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import IntEnum


class Severity(IntEnum):
    """Ordered so that ``severity >= threshold`` decides whether a check fails."""

    INFO = 0
    WARNING = 1
    REINDEX = 2
    BREAKING = 3

    @property
    def label(self) -> str:
        return {0: "info", 1: "warning", 2: "re-index required", 3: "breaking"}[self.value]

    @classmethod
    def parse(cls, value: str) -> "Severity":
        key = value.strip().lower().replace("-", "").replace("_", "").replace(" ", "")
        mapping = {
            "info": cls.INFO,
            "warning": cls.WARNING,
            "warn": cls.WARNING,
            "reindex": cls.REINDEX,
            "reindexrequired": cls.REINDEX,
            "breaking": cls.BREAKING,
            "error": cls.BREAKING,
        }
        if key not in mapping:
            raise ValueError(f"unknown severity {value!r}")
        return mapping[key]


@dataclass
class Finding:
    rule: str
    severity: Severity
    message: str
    contract: str | None = None
    field: str | None = None
    file: str | None = None
    line: int | None = None
    hint: str | None = None
    system: str | None = None  # the AI system a governance finding is about
    provider: str | None = None  # the model provider a governance finding is about
    # Already present on the base branch: reported, but never fails the check.
    preexisting: bool = False

    def key(self) -> tuple:
        return (self.rule, self.contract, self.field, self.system, self.provider)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["severity"] = self.severity.label
        return {k: v for k, v in data.items() if v is not None and v is not False}


@dataclass
class ContractResult:
    contract_id: str
    contract_path: str
    findings: list[Finding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)  # tool limitations, not policy findings
    status: str = "unchanged"  # new | changed | unchanged | removed | invalid | systems


@dataclass
class CheckResult:
    results: list[ContractResult] = field(default_factory=list)
    base_label: str | None = None
    head_label: str = "working tree"
    explanation: str | None = None

    @property
    def findings(self) -> list[Finding]:
        return [f for r in self.results for f in r.findings]

    def count(self, severity: Severity) -> int:
        return sum(1 for f in self.new_findings if f.severity == severity)

    @property
    def new_findings(self) -> list[Finding]:
        return [f for f in self.findings if not f.preexisting]

    def worst(self) -> Severity | None:
        return max((f.severity for f in self.new_findings), default=None)

    def fails(self, threshold: Severity | None) -> bool:
        worst = self.worst()
        return threshold is not None and worst is not None and worst >= threshold

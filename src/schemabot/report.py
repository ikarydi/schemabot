"""Render a check result as a PR comment, plain text, JSON or SARIF."""

from __future__ import annotations

import json

from . import __version__
from .findings import CheckResult, Finding, Severity
from .rules import RULES

MARKER = "<!-- schemabot-report -->"

_ICON = {
    Severity.BREAKING: "🛑",
    Severity.REINDEX: "🔁",
    Severity.WARNING: "⚠️",
    Severity.INFO: "ℹ️",
}


def _verdict(result: CheckResult, threshold: Severity | None) -> str:
    if result.fails(threshold):
        return "🛑 **Blocking:** this PR breaks a data contract."
    worst = result.worst()
    if worst == Severity.REINDEX:
        return "🔁 **Passes, but needs a re-index** before or right after merging."
    if worst == Severity.WARNING:
        return "⚠️ **Passes with warnings.**"
    return "✅ **All data contracts hold.**"


def _summary_line(result: CheckResult) -> str:
    parts = []
    for sev in (Severity.BREAKING, Severity.REINDEX, Severity.WARNING, Severity.INFO):
        n = result.count(sev)
        if n:
            parts.append(f"{n} {sev.label}")
    checked = sum(1 for r in result.results if r.status != "removed")
    return f"{checked} contract{'s' if checked != 1 else ''} checked" + (": " + ", ".join(parts) if parts else "")


def _location(f: Finding) -> str:
    if not f.file:
        return ""
    return f"`{f.file}:{f.line}`" if f.line else f"`{f.file}`"


def _escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def to_markdown(result: CheckResult, threshold: Severity | None = Severity.BREAKING) -> str:
    lines = [MARKER, "## SchemaBot", "", _verdict(result, threshold), "", _summary_line(result)]
    if result.base_label:
        lines[-1] += f" (compared with {result.base_label})."
    else:
        lines[-1] += "."

    if result.explanation:
        lines += ["", "### What this means", "", result.explanation.strip()]

    for res in result.results:
        new = [f for f in res.findings if not f.preexisting and f.severity > Severity.INFO]
        info = [f for f in res.findings if not f.preexisting and f.severity == Severity.INFO]
        old = [f for f in res.findings if f.preexisting]
        if not (new or info or old or res.warnings):
            continue
        status = "" if res.status in ("unchanged", "checked") else f" · {res.status}"
        lines += ["", f"### `{res.contract_id}`{status}", ""]
        if new:
            lines += ["| | Rule | Finding | Where |", "|---|---|---|---|"]
            for f in new:
                msg = _escape(f.message) + (f"<br>↳ {_escape(f.hint)}" if f.hint else "")
                lines.append(f"| {_ICON[f.severity]} | `{f.rule}` | {msg} | {_location(f)} |")
        if info:
            lines += ["", "<details><summary>Notes</summary>", ""]
            lines += [f"- {f.message}" for f in info]
            lines += ["", "</details>"]
        if old:
            lines += ["", f"<details><summary>{len(old)} pre-existing finding(s), not blocking</summary>", ""]
            lines += [f"- {_ICON[f.severity]} `{f.rule}`: {f.message}" for f in old]
            lines += ["", "</details>"]
        if res.warnings:
            lines += ["", "<details><summary>Analysis notes</summary>", ""]
            lines += [f"- {w}" for w in res.warnings]
            lines += ["", "</details>"]

    lines += [
        "",
        f"<sub>SchemaBot {__version__} · rules are deterministic; any summary above is AI-written "
        f"and never decides pass or fail.</sub>",
    ]
    return "\n".join(lines) + "\n"


def to_text(result: CheckResult, threshold: Severity | None = Severity.BREAKING) -> str:
    out = [_summary_line(result)]
    for res in result.results:
        for f in res.findings:
            if f.severity == Severity.INFO and not f.preexisting:
                continue
            tag = "pre-existing " if f.preexisting else ""
            where = f" [{f.file}{':' + str(f.line) if f.line else ''}]" if f.file else ""
            out.append(f"  {tag}{f.severity.label.upper():<17} {res.contract_id}: {f.rule}: {f.message}{where}")
        for w in res.warnings:
            out.append(f"  note              {res.contract_id}: {w}")
    out.append("FAIL" if result.fails(threshold) else "PASS")
    return "\n".join(out) + "\n"


def to_json(result: CheckResult, threshold: Severity | None = Severity.BREAKING) -> str:
    payload = {
        "version": __version__,
        "passed": not result.fails(threshold),
        "base": result.base_label,
        "counts": {s.label: result.count(s) for s in Severity},
        "contracts": [
            {
                "id": r.contract_id,
                "path": r.contract_path,
                "status": r.status,
                "findings": [f.to_dict() for f in r.findings],
                "notes": r.warnings,
            }
            for r in result.results
        ],
        "explanation": result.explanation,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


_SARIF_LEVEL = {
    Severity.BREAKING: "error",
    Severity.REINDEX: "warning",
    Severity.WARNING: "warning",
    Severity.INFO: "note",
}


def to_sarif(result: CheckResult, path_prefix: str = "") -> str:
    """SARIF 2.1.0, for GitHub code scanning annotations.

    ``path_prefix`` turns paths relative to the checked root into paths relative
    to the repository root, which is what code scanning expects.
    """
    prefix = path_prefix.strip("/")

    def uri(path: str) -> str:
        return f"{prefix}/{path}" if prefix else path

    rules = [
        {
            "id": r.id,
            "name": "".join(part.title() for part in r.id.split("-")),
            "shortDescription": {"text": r.title},
            "defaultConfiguration": {"level": _SARIF_LEVEL[r.severity]},
            "properties": {"severity": r.severity.label},
        }
        for r in RULES.values()
    ]
    results = []
    for f in result.findings:
        if f.preexisting:
            continue
        entry: dict = {
            "ruleId": f.rule,
            "level": _SARIF_LEVEL[f.severity],
            "message": {"text": f.message + (f" {f.hint}" if f.hint else "")},
        }
        if f.file:
            region = {"startLine": f.line} if f.line else {"startLine": 1}
            entry["locations"] = [
                {"physicalLocation": {"artifactLocation": {"uri": uri(f.file)}, "region": region}}
            ]
        results.append(entry)
    doc = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "SchemaBot",
                        "version": __version__,
                        "informationUri": "https://github.com/ikarydi/schemabot",
                        "rules": rules,
                    }
                },
                "results": results,
            }
        ],
    }
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"

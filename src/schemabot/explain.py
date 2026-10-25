"""Optional plain-English summary of the findings, written by Claude.

The rule engine has already decided pass or fail before this runs; the summary
only explains it. It is off unless asked for, needs ``pip install schemabot[llm]``
and an Anthropic API key, and any failure here falls back to no summary rather
than failing the check.
"""

from __future__ import annotations

import json
import os

from .contract import Contract
from .findings import CheckResult, Severity

DEFAULT_MODEL = "claude-opus-5-5"
# Models that accept server-side fallback on refusal (``fallbacks: "default"``).
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}

SYSTEM = """You review data-contract findings on a pull request for a team that runs \
RAG and ingestion pipelines. A deterministic rule engine produced the findings; \
you do not change them or add new ones. Write a short summary for the PR author:

- Start with one sentence on what the PR does to the data contracts and whether it is safe to merge.
- For each blocking or re-index finding, say in one or two sentences what would \
go wrong in production (for example, retrieval leaking across tenants, personal \
data stored in a vector index, stale vectors) and the smallest fix.
- If a contract change would fix a finding, include the YAML patch in a fenced block.
- Skip info-level findings unless they matter. No headings, under 200 words, \
plain Markdown."""


def _payload(result: CheckResult, contracts: dict[str, Contract]) -> str:
    items = []
    for res in result.results:
        findings = [f.to_dict() for f in res.findings if not f.preexisting and f.severity > Severity.INFO]
        if not findings:
            continue
        contract = contracts.get(res.contract_id)
        items.append(
            {
                "contract": res.contract_id,
                "rag": contract.rag.model_dump(by_alias=True, exclude_none=True) if contract and contract.rag else None,
                "findings": findings,
            }
        )
    return json.dumps(items, indent=2, ensure_ascii=False)


def explain(result: CheckResult, contracts: dict[str, Contract], model: str | None = None) -> tuple[str | None, str | None]:
    """Return (summary, error). Never raises."""
    if not any(not f.preexisting and f.severity > Severity.INFO for f in result.findings):
        return None, None
    try:
        import anthropic
    except ImportError:
        return None, "the LLM summary needs `pip install schemabot[llm]`"
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return None, "no ANTHROPIC_API_KEY set; skipped the LLM summary"

    model = model or os.environ.get("SCHEMABOT_MODEL") or DEFAULT_MODEL
    extra: dict = {}
    if model in _FALLBACK_MODELS:
        # Retry on Anthropic's recommended model if a safety classifier declines.
        extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    try:
        client = anthropic.Anthropic()
        response = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            system=SYSTEM,
            output_config={"effort": "medium"},
            **extra,
            messages=[
                {
                    "role": "user",
                    "content": "Findings (JSON):\n\n" + _payload(result, contracts),
                }
            ],
        )
    except anthropic.APIStatusError as exc:
        return None, f"LLM summary failed ({exc.status_code}): {exc.message}"
    except anthropic.APIConnectionError as exc:
        return None, f"LLM summary failed: could not reach the API ({exc})"
    if response.stop_reason == "refusal":
        return None, "LLM summary declined by the model"
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    return (text or None), None

# SchemaBot (public)
GitHub Action + CLI that blocks PRs which break data contracts feeding RAG and ingestion pipelines: tenant leaks, PII reaching the embedder, silent vector invalidation, AI-use policy violations.

## Commands
```
python -m pip install -e ".[dev]"
python -m pytest          # CI runs Python 3.10–3.13
schemabot --help
```
- Architecture and decisions: `docs/architecture.md`.

## Rules
- Runs entirely in the customer's CI. Never add telemetry, network calls home, or a hosted dependency.
- A deterministic rule engine decides pass/fail. The optional LLM (`[llm]` extra) only explains a result and never changes it.
- Keep Python 3.10 compatibility.
- Changes to `action.yml` inputs or the contract format are public API. Follow semver, add a CHANGELOG entry, and keep old contracts working or ship a migration note.
- This repo is public: no client names, client data, or secrets in code, tests, examples or commits.

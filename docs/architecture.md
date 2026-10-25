# Architecture and decisions

SchemaBot's MVP is a Python GitHub Action plus CLI that runs entirely inside
the customer's CI. A deterministic rule engine decides pass or fail; an LLM can
optionally explain the result but never changes it.

```
pull request ──► GitHub Actions runner (customer's repo, no SchemaBot servers)
                 │
                 ├─ contracts/*.yaml ───────────────────────────────┐
                 ├─ migrations / models ─► extract (base and head) ─┤
                 └─ ingestion code ──────► embed-scope scan ────────┤
                                                                    ▼
                                                     rule engine (rules.py)
                                                                    │
                                     report: PR comment, job summary, JSON, SARIF
                                                                    │  ◄── optional LLM summary
                                                                    ▼
                                                merge passes, warns or blocks
```

| Module | Role |
|---|---|
| `contract.py` | ODCS v3 subset + `rag` block; Pydantic model, loader, validation. |
| `snapshot.py` | Working tree and git revision views with one interface. |
| `extract/sql.py` | Replays SQL migrations with sqlglot into a table shape. |
| `extract/pydantic_model.py` | Reads model classes with `ast`, without importing them. |
| `scan.py` | Finds embed calls and traces which fields reach them. |
| `rules.py` | Every rule, its severity, and the finding text. |
| `check.py` | Runs base vs head, marks pre-existing findings. |
| `report.py` | Markdown, text, JSON and SARIF output. |
| `explain.py` | Optional Claude summary (`schemabot[llm]`). |
| `cli.py` / `action.yml` | Entry points. |

## Decisions (October 2026)

These were the open questions in the architecture draft. J approved building
the MVP on 2026-10-25 with the RAG niche and the recommended defaults for the
rest.

| # | Decision | Chosen | Why |
|---|---|---|---|
| 1 | Niche | Contracts for RAG and ingestion pipelines | Warehouse contracts are served by Gable, Monte Carlo and Atlan; agent governance is crowded. RAG contracts are unclaimed. |
| 2 | Delivery | GitHub Action + CLI; hosted GitHub App later | No backend to run, nothing leaves the customer, one-PR install. |
| 3 | Contract format | ODCS v3 subset + `rag` extension | Credible with data teams; import/export comes free. |
| 4 | First sources | SQL migrations (Postgres by default) and Pydantic models | Where most RAG apps keep their sources; both parse offline. dbt and live introspection come next. |
| 5 | Role of the LLM | Deterministic core; LLM only explains, with the user's key | A CI gate must be reproducible. |
| 6 | Stack | Python 3.10+, pydantic, sqlglot, PyYAML | The data-engineering ecosystem is Python. |
| 7 | Licence / model | Open core, Apache 2.0 | Distribution through GitHub; charge later for the hosted App, history and alerts. |
| 8 | LogicLens | Deferred | Keep the first build on one wedge. |

## Next steps after the MVP

- dbt manifest and live database extractors.
- Hosted GitHub App: org-wide contract registry, history, Slack alerts.
- Scheduled drift check against production (contract vs live schema).
- More embed sinks (LlamaIndex ingestion pipelines, Haystack) and a JS/TS scanner.

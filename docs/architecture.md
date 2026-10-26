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
| `governance.py` | AI systems manifest and org policy pack; models and loaders. |
| `providers.py` | Finds model provider clients, regions and fine-tuning calls in pipeline code. |
| `lineage.py` | Builds the contract → AI system → provider graph; text, JSON and Mermaid output. |
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

## AI data governance decisions (October 2026)

J asked for an AI data governance extension on 2026-10-26 and approved the
recommended option for each open question the same day. The full design is in
the governance architecture doc.

| # | Decision | Chosen | Why |
|---|---|---|---|
| G1 | Slice | AI data use, checked at build time | Runtime agent governance is crowded and being bundled by the platforms; model GRC is sold top-down. No one enforces data use in CI. |
| G2 | Where AI systems are declared | A separate `ai-systems.yaml` | One system reads many contracts; platform teams own systems, data owners own contracts. |
| G3 | Uses covered | RAG, fine-tuning and eval | Fine-tuning on customer data is what lawyers ask about; prompt-context tracing is too dynamic for now. |
| G4 | Org policy pack | Local `policies.yaml` first | A central pack becomes a hosted-App feature. |
| G5 | Erasure check | Static check that the declared deletion reaches the vector store | A runtime deletion test comes later as a scheduled job. |
| G6 | Approval gate | SchemaBot reads PR reviews and blocks | Falls back to a warning when the token cannot read reviews. |
| G7 | Compliance framing | EU AI Act + GDPR evidence | ISO/IEC 42001 and NIST AI RMF mappings later; say "evidence", never "compliant". |
| G8 | Positioning | One product, SchemaBot with governance | One install, one contract. |

Build phases: (1) governance block, manifest, policy pack and the first rules
(v0.2.0); (2) provider and region detection, lineage graph (v0.3.0); (3) erasure
and retention; (4) approvals, `schemabot inventory` and `schemabot evidence`;
(5) hosted App features.

## Next steps after the MVP

- dbt manifest and live database extractors.
- Hosted GitHub App: org-wide contract registry, history, Slack alerts.
- Scheduled drift check against production (contract vs live schema).
- More embed sinks (LlamaIndex ingestion pipelines, Haystack) and a JS/TS scanner.

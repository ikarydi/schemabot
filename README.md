# SchemaBot

**Data contracts for RAG and ingestion pipelines, checked on every pull request.**

SchemaBot is a GitHub Action and CLI. It reads a small YAML contract for each
source your retrieval pipeline reads, and blocks a pull request that would:

- **leak across tenants**, by dropping or renaming a column the retriever filters on;
- **put personal data in a vector index**, by sending a PII field to the embedder;
- **silently invalidate your vectors**, by changing the embedding model, dimension,
  chunking or the set of embedded fields without a re-index.

It also catches the classic contract breaks (missing fields, type changes, a
required field becoming nullable) and flags new columns that look like personal
data.

Everything runs inside your own CI. No data, credentials or code leave the
runner, and there is no SchemaBot server.

## Quick start

1. Add a contract next to your pipeline code, e.g. `contracts/support_tickets.yaml`:

   ```yaml
   apiVersion: v3.0.0
   kind: DataContract
   id: support-tickets
   owner: support-platform
   source:
     type: postgres
     schemaFrom: migrations/          # replayed in file-name order
     table: public.support_tickets
   schema:
     - {name: id,             type: uuid,      required: true, primaryKey: true}
     - {name: tenant_id,      type: uuid,      required: true}
     - {name: subject,        type: text,      required: true}
     - {name: body,           type: text,      required: true}
     - {name: customer_email, type: text,      classification: pii}
     - {name: updated_at,     type: timestamp, required: true}
   rag:
     embed: [subject, body]           # only these may be embedded
     neverEmbed: [customer_email]     # fails the PR if it reaches the embedder
     filterKeys: [tenant_id]          # the retriever must filter on these
     code: [ingest/tickets.py]        # where to look for embed calls
     index:
       model: text-embedding-3-small
       dimension: 1536
       chunking: {strategy: recursive, size: 800, overlap: 100}
   ```

2. Add the workflow, `.github/workflows/schemabot.yml`:

   ```yaml
   name: SchemaBot
   on: pull_request
   permissions:
     contents: read
     pull-requests: write   # for the PR comment
   jobs:
     contracts:
       runs-on: ubuntu-latest
       steps:
         - uses: actions/checkout@v5
           with:
             fetch-depth: 0
         - uses: ikarydi/schemabot@main
   ```

3. Open a pull request. SchemaBot posts one comment, updates it on every push,
   and fails the check when a contract breaks.

A full working example lives in [`examples/rag-demo`](examples/rag-demo).

## What a report looks like

> 🛑 **Blocking:** this PR breaks a data contract.
>
> | | Rule | Finding | Where |
> |---|---|---|---|
> | 🛑 | `filter-key-missing` | `tenant_id` is a retriever filter key but no longer exists in `public.support_tickets`. Retrieval would stop filtering by it and could return another tenant's documents.<br>↳ Looks like a rename to `org_id`; update the contract and every consumer together. | `migrations/003_org.sql` |
> | 🛑 | `never-embed-violation` | `customer_email` reaches the embedder via `Document`, but the contract says it must never be embedded. | `ingest/tickets.py:15` |
> | 🔁 | `index-changed` | Index settings changed (dimension: 1536 → 3072; model: 'text-embedding-3-small' → 'text-embedding-3-large'). | `contracts/support_tickets.yaml` |
> | ⚠️ | `possible-pii` | New column `phone_number` looks like personal data and is not classified in the contract. | `migrations/003_org.sql` |

## Contract reference

| Key | Meaning |
|---|---|
| `id` | Unique contract id; used to match the contract between base and head. |
| `owner` | Team that owns the source (free text). |
| `source.type` | `postgres` / `sql` (migrations or a schema file) or `pydantic` (a model class). |
| `source.schemaFrom` | A directory of `.sql` migrations (replayed in file-name order, so zero-pad them), a single `.sql` file, or a `.py` file / directory for Pydantic. |
| `source.table` / `source.model` | The table (`schema.table` or `table`) or the model class name. |
| `source.dialect` | sqlglot dialect for SQL sources. Default `postgres`. |
| `schema[]` | Fields: `name`, `type`, `required`, `primaryKey`, `classification` (`pii`, `secret`, `phi`, `pci`, `restricted`, `sensitive` are protected from embedding), `description`. |
| `rag.embed` | Fields that may be chunked and embedded. |
| `rag.neverEmbed` | Fields that must never reach an embedder (sensitive classifications are added automatically). |
| `rag.filterKeys` | Fields the retriever filters on for tenant or access isolation. |
| `rag.code` | Python files or directories to scan for embed calls. |
| `rag.embedQuery` | The SQL that feeds the embedder, when the code is too dynamic to trace. |
| `rag.index` | `model`, `dimension`, `chunking` (any keys). Any change requires a re-index. |

Types are normalized, so `text`, `varchar(255)`, `str` and `string` all match.

## Rules

| Severity | Rule | When |
|---|---|---|
| 🛑 breaking | `filter-key-missing` | A filter key no longer exists in the source. |
| 🛑 breaking | `never-embed-violation` | A never-embed or sensitive field reaches an embedder. |
| 🛑 breaking | `field-missing` | A contract field no longer exists in the source. |
| 🛑 breaking | `type-mismatch` | The source type differs from the contract. |
| 🛑 breaking | `nullability-mismatch` | A required field is nullable in the source. |
| 🛑 breaking | `source-missing` | The table or model was not found. |
| 🛑 breaking | `pii-in-embed-scope` | A sensitive field is listed in `rag.embed`. |
| 🛑 breaking | `filter-key-removed` | A filter key was removed from the contract. |
| 🛑 breaking | `contract-type-changed` | The contract changed a field's type. |
| 🛑 breaking | `contract-guarantee-relaxed` | The contract made a required field optional. |
| 🛑 breaking | `embed-query-star` | `rag.embedQuery` selects `*` while never-embed fields exist. |
| 🛑 breaking | `contract-invalid` | A contract file does not parse or validate. |
| 🔁 re-index | `index-changed` | Embedding model, dimension or chunking changed. |
| 🔁 re-index | `embed-scope-changed` | The set of embedded fields changed. |
| 🔁 re-index | `embedded-type-changed` | An embedded field changed type. |
| ⚠️ warning | `embed-scope-exceeded` | Code embeds a field that is not in `rag.embed`. |
| ⚠️ warning | `possible-pii` | A new column looks like personal data and is not classified. |
| ⚠️ warning | `never-embed-relaxed` | A field lost its never-embed protection. |
| ⚠️ warning | `contract-field-removed` / `contract-removed` | A field or a whole contract was removed. |
| ⚠️ warning | `column-removed` / `column-type-changed` | An undeclared column was dropped or retyped. |
| ⚠️ warning | `no-filter-keys` / `embed-scope-undeclared` | The `rag` block is missing filter keys or the embed list. |
| ℹ️ info | `field-undeclared` / `contract-added` | New column not in the contract; new contract. |

Problems that already exist on the base branch are reported as pre-existing and
never fail the check, so adopting SchemaBot on an imperfect codebase does not
block unrelated pull requests.

## Action inputs

| Input | Default | |
|---|---|---|
| `root` | `.` | Project root inside the repo. |
| `contracts` | `contracts` | Contract directory, relative to `root`. |
| `base` | PR base commit | Revision to compare with. |
| `fail-on` | `breaking` | `breaking`, `reindex`, `warning`, `info` or `never`. |
| `comment` | `true` | Post a sticky PR comment. |
| `explain` | `false` | Add an AI-written summary (needs `anthropic-api-key`). |
| `anthropic-api-key` | | Used only when `explain` is true. |
| `model` | `claude-opus-5-5` | Claude model for the summary. |
| `sarif-file` | | Also write SARIF for code scanning annotations. |

To annotate the diff, write SARIF and upload it:

```yaml
      - uses: ikarydi/schemabot@main
        with:
          sarif-file: schemabot.sarif
      - uses: github/codeql-action/upload-sarif@v3
        if: always()
        with:
          sarif_file: schemabot.sarif
```

## CLI

```bash
pip install git+https://github.com/ikarydi/schemabot
schemabot validate                       # contracts vs. the working tree
schemabot check --base origin/main       # what this branch changes
schemabot check --base origin/main --format markdown --fail-on reindex
```

Exit codes: `0` passed, `1` a finding at or above `--fail-on`, `2` usage or setup error.

## The optional AI summary

With `explain: true` (or `--explain`), SchemaBot asks Claude to explain the
findings in plain English and to suggest a contract patch where one would help.
The rule engine has already decided pass or fail at that point; the summary
never changes the result, and if the API call fails the check carries on
without it. Install with `pip install "schemabot[llm]"` and set
`ANTHROPIC_API_KEY`.

## How it works

1. **Extract** the source's shape at the base and head revisions: SQL
   migrations are replayed with [sqlglot](https://github.com/tobymao/sqlglot);
   Pydantic models are read with `ast`. User code is never imported.
2. **Scan** the ingestion code for embed calls (`embed_documents`,
   `embeddings.create`, `add_texts`, `Document(page_content=...)`, ...) and
   trace which fields flow into them, through local variables, helper
   functions and functions that pass their parameters on to an embedder.
3. **Apply rules** to the contract at base and head, the two schemas and the
   scan result.
4. **Report** as a PR comment, job summary, JSON or SARIF.

See [docs/architecture.md](docs/architecture.md) for the design decisions.

### Known limits

- The embed-scope scan is static and covers common patterns. Code that builds
  field names dynamically is invisible to it; declare `rag.embedQuery` instead.
- SQL sources are read from migrations in the repo, not from a live database.
- GitHub only for now.

## Licence

Apache 2.0. See [LICENSE](LICENSE).

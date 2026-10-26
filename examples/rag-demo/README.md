# RAG demo project

A tiny RAG ingestion project with two contracts, used by SchemaBot's tests and
by its own CI.

- `migrations/` creates `public.support_tickets` (SQL source).
- `app/models.py` defines `KBArticle` (Pydantic source).
- `ingest/` embeds tickets with LangChain and articles with the OpenAI API.
- `contracts/` holds one contract per source, each with a `governance` block.
- `ai-systems.yaml` declares the one AI system (a support copilot) and its inputs.
- `policies.yaml` forbids fine-tuning on PII and any training or eval on PHI,
  and allows PII to reach only OpenAI, Anthropic and local models.

Try it from the repository root:

```bash
pip install -e .
schemabot validate --root examples/rag-demo
```

Then break something and compare with the last commit:

```bash
echo "ALTER TABLE public.support_tickets RENAME COLUMN tenant_id TO org_id;" \
  > examples/rag-demo/migrations/003_org.sql
schemabot check --root examples/rag-demo --base HEAD
```

Or point the copilot at a purpose the contracts do not allow:

```bash
sed -i 's/purpose: agent-assist/purpose: marketing/' examples/rag-demo/ai-systems.yaml
schemabot check --root examples/rag-demo --base HEAD
```

Or move the embedder out of the EU, and print the lineage:

```bash
sed -i 's/region: eu}/region: us-east-1}/' examples/rag-demo/ai-systems.yaml
schemabot check --root examples/rag-demo --base HEAD
schemabot lineage --root examples/rag-demo --format mermaid
```

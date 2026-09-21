# Formulary API

FastAPI backend for chat orchestration, retrieval, tools, and pharmaceutical data sources.

The internal FastMCP server includes provenance-aware openFDA tools for labels, adverse-event
reaction counts, approvals, shortages, recalls, Complete Response Letters, and advanced queries
across every current openFDA dataset. It also includes
`prepare_bioequivalence_evidence_request`, which identifies missing drug, objective, formulation,
route, dose, reference/test product, and company-data context before quantitative research begins.
The chatbot retrieves reference evidence and explains deterministic comparison results; it does
not predict candidate pharmacokinetic values.

The authenticated `POST /api/v1/bioequivalence/analyze` endpoint provides deterministic
test/reference ratios, evaluates supplied 90% confidence intervals against a configurable
acceptance rule, and derives descriptive Cmax, Tmax, and trapezoidal AUC from concentration-time
profiles. FDA-to-company comparisons remain explicitly exploratory unless suitable study
statistics are supplied.

Large FDA results use a durable ingestion path:

- `ingest_openfda_query` stores the full response and records as compressed, content-addressed
  JSON; deduplicates stable records; and creates section-aware LlamaIndex chunks.
- `search_ingested_evidence` returns bounded, source-linked chunks using PostgreSQL full-text
  ranking.
- `read_ingested_document_chunks` pages through a document when exhaustive coverage is required.

Legacy FDA tools enforce a serialized result cap and direct oversized results into the ingestion
path. See [`docs/fda-data-architecture.md`](../../docs/fda-data-architecture.md) for the data map.

## Run locally

From the repository root:

```bash
conda activate formulary
cd apps/api
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

OpenAPI documentation is available at <http://localhost:8000/docs>.

## Chat turns

After authenticating with the email OTP endpoints and creating a conversation, send a turn with:

```bash
curl -N -X POST http://localhost:8000/api/v1/conversations/CONVERSATION_ID/turns \
  -H 'Content-Type: application/json' \
  -b 'formulary_session=SESSION_TOKEN' \
  -d '{"text":"Convert 2500 mcg to mg"}'
```

The response uses server-sent events. Current event types are `run.started`, `tool.started`,
`tool.completed`, `tool.failed`, `message.delta`, `message.completed`, and `run.failed`.

The provider and model come from `DEFAULT_LLM_PROVIDER` and `DEFAULT_LLM_MODEL`. A request may
override them with `provider` and `model`, and a conversation may store defaults in
`model_preferences`.

Transient model-provider timeouts, connection failures, HTTP 429 responses, and HTTP 5xx
responses are retried according to `LLM_TRANSIENT_MAX_RETRIES` and
`LLM_RETRY_BASE_DELAY_SECONDS`. Failed runs always store sanitized diagnostics internally. Set
`CHAT_EXPOSE_ERROR_DETAILS=true` during local or staging troubleshooting to include the provider
message, status, retry count, and request ID in the frontend error toast. Detailed errors are
always suppressed when `APP_ENV=production`, regardless of the switch.

Provider HTTP calls are not made by the automated tests. They use mocked transports; the
integration suite uses the real PostgreSQL database and in-memory FastMCP transport.

openFDA works under a small unauthenticated allowance, but regular development should use the
free `OPENFDA_API_KEY` in the root `.env`. The key is never included in tool provenance or stored
tool results.

## Checks

From `apps/api`:

```bash
ruff check .
mypy app tests
pytest
pytest -m integration
```

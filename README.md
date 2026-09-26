# Formulary

An application-owned pharmaceutical research chatbot with a FastAPI orchestration layer,
FastMCP tools, PostgreSQL/pgvector, and a responsive Next.js chat interface.

## Stack

- FastAPI, SQLAlchemy 2, Alembic, and async Psycopg
- PostgreSQL 18 with pgvector in the same Docker container
- OpenAI- and Groq-compatible LLM providers
- FastMCP client/server integration
- LlamaIndex Core for section-aware ingestion transformations
- Local BGE-small ONNX embeddings with PostgreSQL full-text/pgvector hybrid retrieval
- FDA reference-evidence and bioequivalence intake that asks only for missing high-impact context
- Provenance-aware openFDA tools spanning all current FDA API datasets
- Content-addressed raw FDA storage, versioned PostgreSQL chunks, deduplication, and full-text search
- Deterministic test/reference endpoint assessment with 90% confidence-interval criteria,
  concentration-time plots, and controlled simulator-neutral JSON export
- Next.js 16, React 19, TypeScript, Tailwind CSS 4, and shadcn-style components
- Passwordless email OTP authentication

## Start locally

### 1. Database

Create the root environment file if needed, set a local database password, then start PostgreSQL:

```bash
cp .env.example .env
docker compose up -d db
docker compose ps
```

The Docker image includes PostgreSQL and pgvector; there is no second vector database to run.

### 2. API

From the repository root:

```bash
uv venv --python 3.13
source .venv/bin/activate
uv pip install -r requirements.txt
cd apps/api
alembic upgrade head
python -m scripts.backfill_embeddings
uvicorn app.main:app --reload --port 8000
```

The virtual environment lives at `.venv` in the repository root and is managed with
[uv](https://docs.astral.sh/uv/). If it already exists, `uv venv` is a no-op and you can go
straight to `uv pip install -r requirements.txt`.

The API is available at <http://localhost:8000> and its OpenAPI UI at
<http://localhost:8000/docs>.

For local OTP testing without SMTP, set `EMAIL_DELIVERY_MODE=console`; the code is printed in
the API terminal. For real email delivery, retain `smtp` and fill the SMTP variables in `.env`.
Also configure either the OpenAI or Groq variables and select the default provider/model.

### 3. Web app

In a second terminal:

```bash
cd apps/web
cp .env.local.example .env.local
pnpm install
pnpm dev
```

Open <http://localhost:3000>. The generic FDA chatbot is available at `/chat` and the
bioequivalence workspace at `/bioequivalence`. The frontend talks directly to the API URL
configured by `NEXT_PUBLIC_API_BASE_URL`.

## Answer-quality evaluation

`apps/api/scripts/eval_chat.py` grades real answers against checks in the terminal.
`apps/api/scripts/eval_langsmith.py` runs the same cases as a LangSmith experiment, scored with
RAGAS metrics (faithfulness, answer relevancy, context utilisation and, once reference
answers are written in the LangSmith UI, context recall and factual correctness) alongside the
project's own rule and numeric-grounding checks. Both call the live model and sources.

```bash
uv pip install -r requirements.txt -r requirements-eval.txt
# set LANGSMITH_API_KEY in .env
cd apps/api
python -m scripts.eval_langsmith --sync-only    # create the dataset
python -m scripts.eval_langsmith                # run an experiment
```

## Quality checks

```bash
source .venv/bin/activate
cd apps/api
ruff check .
mypy app tests
pytest
pytest -m integration

cd ../web
pnpm lint
pnpm typecheck
pnpm build
```

Integration tests require the Docker database. Durable ingestion, local BGE embedding segments,
HNSW vector indexing, and hybrid PostgreSQL retrieval are implemented.

How answers stay grounded - structured queries, citation guarantees, web search, and the
live evaluation harness - is documented in [`docs/retrieval-quality.md`](docs/retrieval-quality.md).
Regulatory intelligence - change detection across FDA and ClinicalTrials.gov, the News tab,
and email/Slack watches - is documented in
[`docs/regulatory-intelligence.md`](docs/regulatory-intelligence.md).
The upstream transport layer shared by every evidence source - rate limiting, retry policy,
and what it takes to add a new source - is documented in
[`docs/upstream-sources.md`](docs/upstream-sources.md).
The FDA retrieval design, endpoint inventory, API limits, future cache/indexing flow, and
layout-aware OCR direction are documented in
[`docs/fda-data-architecture.md`](docs/fda-data-architecture.md).
The bioequivalence modes, formulas, limitations, and export boundary are documented in
[`docs/bioequivalence-workspace.md`](docs/bioequivalence-workspace.md).

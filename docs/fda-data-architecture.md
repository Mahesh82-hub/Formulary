# FDA data and MCP architecture

## Purpose

The FDA integration is a retrieval and provenance layer for pharmaceutical research. It is
designed to support regulatory research, safety-signal exploration, formulation and substance
research, and future in-silico study workflows. It must not turn public reports into unsupported
causal or clinical claims.

openFDA is an Elasticsearch-backed API. We use its native fielded search, exact fields, Boolean
expressions, ranges, wildcards, counts, sorting, and pagination instead of copying the entire
dataset into PostgreSQL prematurely.

## Current openFDA surface

The allowlist is generated from the current official download manifest and covers every available
endpoint:

| Area | Datasets |
| --- | --- |
| Human drugs | `drug/event`, `drug/label`, `drug/ndc`, `drug/drugsfda`, `drug/shortages`, `drug/enforcement`, `drug/orangebook` |
| Medical devices | `device/510k`, `device/classification`, `device/enforcement`, `device/event`, `device/pma`, `device/recall`, `device/registrationlisting`, `device/udi`, `device/covid19serology` |
| Regulatory transparency | `transparency/crl`, `other/historicaldocument` |
| Substance and listing data | `other/nsde`, `other/substance`, `other/unii` |
| Other FDA-regulated products | `food/event`, `food/enforcement`, `cosmetic/event`, `animalandveterinary/event` |
| Tobacco research | `tobacco/problem`, `tobacco/researchdigitalads`, `tobacco/researchpreventionads`, `tobacco/researchsmokefree` |

The download manifest at `https://api.fda.gov/download.json` also exposes partition URLs,
export dates, file sizes, and record counts. That manifest is the correct entry point for a future
bulk ingestion job.

## MCP tools

The internal FastMCP server exposes:

- `openfda_query`: advanced access to every allowlisted dataset using native openFDA query
  syntax, including aggregations through `count`.
- `get_fda_drug_labels`: selected SPL label sections and their corresponding `_table` fields.
- `analyze_fda_adverse_event_reactions`: top FAERS MedDRA reaction counts for a drug, with
  explicit causal and incidence limitations.
- `search_fda_drug_approvals`: Drugs@FDA applications, products, submissions, and document
  metadata.
- `search_fda_drug_shortages`: current and historical shortage records.
- `search_fda_drug_recalls`: enforcement reports, classification, reason, firm, and dates.
- `search_fda_complete_response_letters`: CRL metadata, bounded text excerpts, and source PDFs.

The focused tools deliberately bound large text fields before returning them to an LLM. The
canonical API query remains in the provenance object, so a later ingestion worker can retrieve
and preserve the complete source record.

## Provenance contract

Each tool result carries:

- the dataset and normalized query parameters;
- the canonical API URL without the API key;
- retrieval timestamp;
- openFDA endpoint update date;
- total and returned result counts;
- disclaimer, license, and terms URLs when supplied by FDA;
- a sanitized next-page URL when openFDA returns a `Link` header;
- dataset-specific caveats.

This contract is intended to remain stable when caching and indexing are added. It lets us hash a
source using the dataset, canonical URL, record identifier, and endpoint update date.

## API behavior and limits

- All requests use HTTPS and the `api.fda.gov` host.
- A free API key raises the daily allowance from 1,000 unauthenticated requests per IP to 120,000
  requests per key. Both modes currently allow 240 requests per minute.
- Standard query parameters are `search`, `sort`, `count`, `limit`, and `skip`.
- openFDA generally permits up to 1,000 results in one call, but individual endpoints can have
  smaller limits. The MCP boundary caps records at 25 and the broad LLM tool at 10 to control
  context size and API usage.
- `skip` supports offsets through 25,000. For result sets beyond 26,000 hits, openFDA supports a
  `search_after` continuation URL through the HTTP `Link` header.
- For complete or repeated large-scale analysis, use the download manifest rather than walking
  the real-time API. A refreshed endpoint can change historical records, so FDA recommends
  re-downloading all partitions for that endpoint rather than assuming append-only updates.

## Relevance to in-silico studies

FDA data can provide valuable inputs and constraints:

- label clinical pharmacology, pharmacokinetics, clinical studies, contraindications, warnings,
  and tabular SPL sections;
- approval histories, review and submission document links, dosage forms, routes, and active
  ingredients;
- CRL deficiencies that can reveal recurring safety, efficacy, manufacturing, statistical, and
  trial-design concerns;
- FAERS reporting patterns for hypothesis generation and post-market surveillance;
- recalls, shortages, regulatory status, Orange Book equivalence and patent-related data;
- UNII and substance records for identity reconciliation.

These sources do not by themselves form a clinical-trial simulator. FAERS lacks a suitable
exposure denominator and cannot establish causality. openFDA also does not replace patient-level
trial data, a study protocol, PK/PD models, disease-progression models, or external chemical and
biological evidence. Future study workflows should join FDA provenance with sources such as
ClinicalTrials.gov and appropriate compound, literature, and model datasets.

## Implemented cache and text-indexing pipeline

1. `ingest_openfda_query` queries openFDA without placing the complete response in LLM context.
2. Complete responses and records are gzip-compressed in content-addressed local storage.
3. Stable logical documents deduplicate by openFDA identifiers; immutable revisions deduplicate by
   SHA-256 checksum.
4. LlamaIndex Core transforms top-level JSON sections into bounded citation chunks.
5. PostgreSQL stores versioned chunks, JSON-path metadata, token counts, and generated `tsvector`
   columns with a GIN index.
6. BGE-small creates model-safe 384-dimensional segments with an HNSW cosine index; hybrid
   retrieval fuses vector and full-text ranks and returns authoritative parent chunks.
7. `read_ingested_document_chunks` provides bounded cursor paging for exhaustive reading.
8. Durable ingestion jobs preserve requests, character/byte/chunk counts, indexing state,
   failures, and raw-response object URIs.

The MCP call currently performs fetch and chunking synchronously while recording a durable job.
Moving job execution into a separately deployed worker is the next operational step before bulk
partition ingestion.

Structured pharmaceutical fact extraction, versioned summaries, and reranking remain future
steps. Local embeddings and pgvector indexing are implemented.

PostgreSQL holds cache metadata, normalized identifiers, ingestion jobs, and text indexes.
Large raw downloads and source documents should move to S3-compatible object storage in
production even though local filesystem storage is sufficient during development.

## Document and OCR direction

FDA JSON and SPL label fields should be consumed directly; OCR would reduce their quality. PDF
sources such as review documents and CRLs require a staged extraction pipeline:

1. Try native PDF text and table extraction first.
2. Detect pages or regions with missing text, scans, figures, or broken reading order.
3. Apply layout-aware OCR only to those regions.
4. Preserve table structure: title, headers, row labels, units, footnotes, merged cells, and page
   coordinates.
5. Normalize numeric values separately from the source representation while retaining the exact
   source text.
6. Validate high-impact extracted values with range, unit, and cross-table consistency checks.
7. Store extraction confidence and make low-confidence values visibly reviewable.

Vector chunks alone are insufficient for tables. Extracted measurements should also be stored as
structured facts linked back to the document, page, table, row, and cell.

## Official references

- <https://open.fda.gov/apis/>
- <https://open.fda.gov/apis/query-syntax/>
- <https://open.fda.gov/apis/query-parameters/>
- <https://open.fda.gov/apis/paging/>
- <https://open.fda.gov/apis/authentication/>
- <https://open.fda.gov/apis/downloads/>
- <https://api.fda.gov/download.json>
- <https://open.fda.gov/apis/drug/drugsfda/>
- <https://open.fda.gov/apis/drug/event/>
- <https://open.fda.gov/apis/drug/label/>
- <https://open.fda.gov/apis/transparency/completeresponseletters/>

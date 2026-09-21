# Upstream sources

Dr. Insilico treats every external evidence provider as an *upstream source*. openFDA is the
first one, but the transport layer is deliberately source-agnostic so PubMed,
ClinicalTrials.gov, DailyMed, RxNorm, and other free sources reuse it rather than each growing
their own retry and pacing code.

## Layering

```text
tool (MCP)  ->  source client  ->  ResilientRequester  ->  httpx
                (openFDA, ...)     (pacing + retries)
```

- `app/sources/resilience.py` owns rate limiting and retries. It must never import a
  source-specific module.
- `app/sources/profile.py` declares each source's published limits as a `SourceProfile`.
- `app/fda/client.py` is the first consumer and remains responsible for openFDA query syntax,
  validation, and provenance.

## Rate limiting

Each source gets an `AsyncTokenBucket` sized from its published ceiling. A token bucket
smooths bursts rather than serialising calls, which matters because the orchestrator can issue
several tool calls concurrently within one turn.

The bucket lives on the requester, which is built once per process and cached, so every call
through a source shares one budget. Limits are typically enforced per API key or per IP, so
each additional process sharing a key consumes the same allowance; horizontal scaling needs a
shared limiter rather than a per-process one.

## Retries

Exponential backoff with full jitter, bounded by `max_attempts`. Full jitter is used rather
than fixed backoff so that concurrent callers recovering from the same outage do not resynchronise
into a thundering herd.

Retryable: `429`, `500`, `502`, `503`, `504`, connection failures, and timeouts.
Not retryable: everything else. `404` in particular means "no matches" for several openFDA
datasets, so retrying it would waste the rate budget and slow the turn.

A `Retry-After` header wins over computed backoff when present, clamped to
`max_retry_after_seconds` so a hostile or mistaken value cannot stall a request indefinitely.
Only the delay-seconds form is honoured; the HTTP-date form is ignored because clock skew
between the client and the source makes it unreliable.

Retries consume rate-limit tokens like any other request. This is intentional: a retry that
skipped the limiter would amplify the overload that caused the failure.

## Federated search

Every question that is not already narrowed to one dataset goes to `search_all_sources`, which
asks every registered source at once.

* **Parallel.** Sources are queried concurrently, so total latency tracks the slowest source
  that answers within its deadline, not the sum of all of them. Adding a source does not make
  answers slower.
* **Independent.** A source that fails, times out, or is misconfigured cannot remove another
  source's evidence from the answer. Each source is given the treatment it would get if it were
  the only one being searched.
* **Live first.** Previously ingested evidence is registered as one more source and enriches an
  answer; it never substitutes for a live query. A cached snapshot is fine for a label section
  and actively wrong for a shortage or a recall.
* **Attributable.** Every record carries its own provenance, and every source asked reports an
  outcome. `empty` means the source had no match and is not a failure; `timeout` and `error`
  raise a caveat instructing the model to state that the answer is incomplete and name the
  missing sources.

Each openFDA dataset registers as its own searcher rather than hiding behind one "FDA" entry,
because a label and a recall answer different questions and a citation should say which.

Results are merged with reciprocal rank fusion. Records are keyed per source, so two sources
describing the same subject stay two citable pieces of evidence rather than being collapsed
into one.

## Adding a source

1. Declare a `SourceProfile` with the source's published rate limit.
2. Write a client that builds requests and sends them through `profile.build_requester()`.
3. Translate `UpstreamError` subclasses into a source-specific error at the client boundary,
   so tool callers see a stable error type.
4. Register a `DataSource` row so ingested documents reuse the existing chunking, embedding,
   and hybrid retrieval path unchanged.

Step 4 currently requires widening the `kind` check constraint on `data_sources`, which still
enumerates `openfda`, `upload`, `website`, and `internal`. `FDAProvenance.source` is also
pinned to `openFDA`, and `EvidenceDataset` only admits openFDA dataset names. These three are
the remaining blockers to a second source.

## Per-source configuration

Sources declare their defaults in code. Only openFDA is wired to environment variables today,
because it is the source under active tuning. Resist adding a full block of variables per
source: prefer the declared profile, and promote a value to settings only when an operator
genuinely needs to change it without a deploy.

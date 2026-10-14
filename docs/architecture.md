# Souq architecture

This document explains how the pieces fit and why they are shaped the way they are. The wire-level contracts
(topic names, headers, payloads, API shapes, telemetry names) are listed in [CLAUDE.md](../CLAUDE.md); this
document doesn't repeat them.

## Components

| Component | Runtime | Responsibility |
|---|---|---|
| `apps/storefront` | Next.js (App Router), server-rendered | Search UI in English and Arabic. Talks only to the gateway's GraphQL endpoint, always from the server. |
| `apps/gateway` | Node 24, Fastify 5, GraphQL Yoga | Public edge: REST `/v1` + GraphQL, API keys, rate limits, Redis response cache, signed page tokens. |
| `services/search` | Python, FastAPI | Owns ranking: normalise → parse → BM25 ∥ kNN → RRF → rerank → paginate. |
| `services/feeds` | Python, FastAPI | Accepts supplier feeds and streams rows into Postgres plus the outbox in one transaction per batch. |
| `services/outbox-relay` | Python, confluent-kafka | Moves committed outbox rows to `catalog.changes`. |
| `services/indexer` | Python, confluent-kafka | Consumes changes, embeds, bulk-indexes into every index behind `products_write`; retry topics and DLQ. |
| Postgres 16 | | Source of truth: `products`, `product_texts`, `outbox`, feed jobs and row errors. |
| Redpanda | | Kafka API. `catalog.changes` (12 partitions, key = sku), two retry tiers, DLQ. |
| OpenSearch 2.x | | `products_vN` indices behind the `products_read` / `products_write` aliases. |
| Redis Stack | | Gateway response cache; RediSearch vector index for the semantic LLM-parse cache. |

## Write path: feed → searchable

1. **Import.** `services/feeds` parses CSV/JSONL incrementally, validates each row against the feed contract
   (rejects carry a code such as `invalid_price` or `duplicate_sku_in_feed`) and upserts `products` +
   `product_texts`. In the **same transaction** it inserts one `outbox` row per effective change, with
   `payload = product_document(sku)` and the current W3C trace context in `outbox.trace_context`.
   `products.version` increments only when something actually changed, so a re-imported feed produces no
   events.
2. **Relay.** Each `services/outbox-relay` replica loops: lock up to `relay.batch_size` unpublished rows with
   `FOR UPDATE SKIP LOCKED`, produce them to `catalog.changes` (key = sku), wait for every ack (`acks=all`),
   mark them published and commit. A crash before the commit means the rows are produced again, so delivery is
   at-least-once. Two replicas lock disjoint rows, which gives throughput and failover without a leader
   election.
3. **Index.** `services/indexer` consumes in batches sized by an AIMD controller. Batches grow on success
   and halve on bulk rejections (429 / `es_rejected_execution_exception`), with backoff. It resolves
   `products_write` to its concrete indices and bulk-writes to all of them with `version_type=external`,
   `version = products.version`.
   - A **409 version conflict is success**: a newer version is already indexed. That makes duplicates from
     at-least-once delivery harmless, and also out-of-order arrival. Two relay replicas can publish v4 of a SKU
     before v3; v3 then gets a 409 and the index still converges to v4.
   - **Retryable** item failures go to `catalog.retry.30s` and then `catalog.retry.5m`, with `attempt` and
     `not_before` headers. **Non-retryable** ones (mapping errors, bad documents) and exhausted retries go to
     `catalog.dlq` with `error_class` / `error_message`. The DLQ admin CLI (`souq-dlq list | inspect |
     redrive`) shows them and republishes them to `catalog.changes` with the attempt count reset.
4. **Freshness** is measured end to end (`index_freshness_seconds`, from `outbox_created_at` to the
   acknowledged bulk write). Target: p95 ≤ 30 s.

### Why a transactional outbox instead of dual writes

Writing to Postgres and Kafka independently can lose an event or invent one whenever either write fails.
With the outbox, the product change and its event commit or roll back together, and the relay only ever
publishes committed facts. Each event is a full document, not a diff, so the indexer never reads Postgres and
any single event is enough to rebuild that SKU's document.

### Why the outbox relay is Python

The relay is a small loop (poll, produce, ack, commit), so it could run in any language. It is Python because
everything it does is already done the same way by the indexer:

- **One Kafka client to tune.** The relay and the indexer both use confluent-kafka, i.e. librdkafka, with the
  same idempotent-producer contract (`enable.idempotence=true`, `acks=all`,
  `max.in.flight.requests.per.connection=5`, lz4, linger). The indexer is itself a producer for retry and DLQ
  topics. One client means one set of configuration keys, one statistics format and one set of failure modes
  to learn (local queue full, delivery timeout, fenced idempotent producer). The producer settings and the
  acknowledged-produce helper are written once, in `souq_common.kafka` (`producer_config()`,
  `produce_and_wait()`), and imported by both services, so tuning happens in one place. A TypeScript relay would add
  KafkaJS or node-rdkafka beside it, with a different retry and idempotence model to keep consistent.
- **Trace propagation written once.** `souq_common.telemetry.inject_kafka_headers()` /
  `extract_kafka_context()` / `context_from_carrier()` implement the W3C `traceparent`/`tracestate` header
  encoding. The relay turns `outbox.trace_context` into Kafka headers and the indexer reads them back with the
  same code. Two implementations would drift (header names, byte encoding, sampling flags), and the result
  would be broken feeds → relay → indexer traces, which is hard to spot.
- **It is I/O-bound.** The work is waiting on Postgres and on broker acks. librdkafka batches, compresses
  and retries on its own C threads, so the GIL and Python's per-message overhead are not the bottleneck at
  `relay.batch_size` = 500 rows per poll. Running two replicas also covers availability.
- **Shared settings and budgets.** `souq_common.settings` / `load_budgets()` give the relay the same env
  contract and the same `config/budgets.yaml` (`relay.poll_interval_ms`, `relay.batch_size`) as the other
  Python services, without a parallel TypeScript config loader.

Alternatives considered:

- **Debezium / Kafka Connect CDC.** A JVM Connect worker is a heavy addition to an 8 GB dev VM. The outbox
  event router would also need custom mapping to turn `trace_context` into the trace headers above.
- **Redpanda Connect.** Lightweight, but it brings a second tracing and configuration story.
- **Relaying from the gateway.** That couples the public read edge to the write pipeline's availability.

The cost of the Python choice: the TypeScript side never produces to Kafka, so Kafka expertise is
concentrated in the Python services. That is acceptable because the gateway's only job is reading.

## Read path: query → results

```
storefront ──GraphQL──▶ gateway ──POST /search──▶ search
                          │                         ├─ normalise   (souq_common.arabic; Arabizi → Arabic)
                          │ Redis response cache    ├─ parse       rule parser → semantic cache → LLM (budgeted)
                          │ page-token sign/verify  ├─ bm25 ∥ (embed → knn)   on alias products_read
                          │                         ├─ fuse        RRF (k = search.rrf_k)
                          ▼                         └─ rerank      int8 cross-encoder over search.rerank_window
                      problem+json errors              → items, facets, parsed_query, degraded, next_cursor
```

- **Normalisation in one place.** `libs/py-common/src/souq_common/arabic.py` is the only implementation of
  tashkeel stripping and letter folding. The OpenSearch index templates mirror it with a `souq_arabic_norm`
  char filter, and a test asserts the two agree. Query-side and index-side normalisation therefore can't drift.
- **Rules first, LLM by exception.** The rule parser handles prices ("under 200", "أقل من ٢٠٠"), brands,
  colours, sizes and categories cheaply. The LLM parser only runs when the query still contains constraint-like
  leftovers. Its results are cached semantically in Redis, so paraphrases share one LLM call. This keeps the
  LLM path at or below 10% of traffic and 1.5k tokens per query.
- **Budgets are enforced, not advisory.** Each stage runs under `asyncio.wait_for` with its budget from
  `config/budgets.yaml`. BM25 runs concurrently with embed + kNN. A stage that misses its budget degrades:
  the reranker falls back to fused order, the LLM parser to the rule parse, and a failed retriever leaves the
  other one serving. The skipped stage is reported in `degraded`; the storefront shows a notice and still
  renders the results.
- **The search service owns ranking and pagination.** Cursors wrap an OpenSearch point-in-time plus
  `search_after` and are opaque to the gateway. The gateway HMAC-signs them into page tokens bound to a hash
  of `q | lang | filters`, so a token can't be replayed against a different query. It never reorders items.
  Paging is forward-only; the storefront implements "previous" by keeping earlier tokens in the URL.
- **Caching.** The gateway caches whole responses in Redis (`gateway.response_cache_ttl_s`) under
  `searchCacheKey(input)`. The search service caches LLM parses (the semantic cache) rather than results, so
  index updates show up without cache invalidation.

## Index lifecycle: zero-downtime reindex

Mappings change by creating a new index (`products_v2`), not by mutating the live one:

1. `scripts/reindex.py` adds `products_v2` to `products_write`. Within one alias refresh (10 s), the indexer
   dual-writes every change to v1 and v2.
2. It backfills v2 from Postgres. External versions make the backfill and live writes commute.
3. It verifies doc counts and spot-checks documents and queries.
4. It swaps `products_read` atomically with one `_aliases` call, then removes v1 from `products_write`, so v1
   stops receiving writes. Rollback (`reindex.py --rollback`) re-attaches v1, catches it up from Postgres
   (a full rebuild if it drifted), verifies it, and swaps back; going forward again only catches up what
   changed while v2 was detached (19 s on 2026-10-14).

## Storefront

- All UI state (query, filters, page tokens) lives in the URL, and pages render on the server. Every view is a
  link, the back button works, and the browser never sees the gateway's address or credentials.
- `/` redirects to `/en` or `/ar` (by `Accept-Language`). `app/[locale]/layout.tsx` is the root layout, so
  `<html lang dir>` comes from the URL segment, with `dir="rtl"` for Arabic. CSS uses only logical properties
  (`margin-inline`, `inset-inline-start`, `text-align: start`), so the layout mirrors without per-direction
  overrides. Product titles use `dir="auto"` because they can fall back to the other language.
- The query interpretation panel shows `parsed_query` as returned: normalised text, detected language and
  script, which parser ran, extracted filters and unparsed tokens. Filters extracted from the query appear as
  non-removable "from your query" chips next to the shopper's own removable chips.

## Observability

One trace per search: the gateway span, then `POST /search`, then the `stage.*` child spans. Each child
records its budget and whether it exceeded it. LLM spans use the `gen_ai.*` semantic conventions with token
usage and never contain prompt or query text; queries appear only as `hash_query()` hashes. On the write path,
the trace context crosses Postgres (`outbox.trace_context`) and Kafka (headers), so one trace covers a feed row
from import to indexing. Metric names are listed in CLAUDE.md; dashboards and the performance report are
maintained with the observability stack (`infra/grafana`, `docs/perf-report.md`).

## Local resource envelope

The whole stack is sized for an 8 GB Docker VM shared with other work. OpenSearch runs with a 768 MB heap,
Redpanda with `--memory=768M --smp=1` and Redis with 256 MB, and every app container has a memory limit in
`infra/compose.apps.yaml`. Test suites that start containers (testcontainers, chaos) run one at a time.

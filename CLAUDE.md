# Souq — bilingual (Arabic/English) catalog search

Polyglot monorepo: pnpm + Turborepo (TypeScript) and a uv workspace (Python). `pnpm turbo test` runs both:
every Python member has a thin `package.json` whose scripts shell out to `uv run`.

## Non-negotiable rules

1. **The gateway never talks to OpenSearch directly.** `apps/gateway` reaches search only through the
   generated client for `services/search` and feeds only through `services/feeds`. No `@opensearch-project/*`
   dependency in any TS package.
2. **The search service owns ranking.** Normalisation, parsing, retrieval, fusion, reranking and pagination
   semantics live in `services/search`. The gateway passes an opaque cursor through; it never reorders items.
3. **All latency budgets live in `config/budgets.yaml`** and are enforced with timeouts (asyncio
   `wait_for` / `AbortSignal.timeout`). No budget literals in code. Python reads via
   `souq_common.budgets.load_budgets()`, TS via `apps/gateway/src/config/budgets.ts`.
4. **Arabic normalisation lives in exactly one module:** `libs/py-common/src/souq_common/arabic.py`.
   OpenSearch analyzers mirror it (char-filter mappings in the index templates) and a test asserts they agree.
   Never re-implement tashkeel/alef/ya/ta-marbuta handling anywhere else.
5. Degrade, don't fail: reranker over budget or down → fused order + `degraded: ["rerank"]`;
   LLM parser timeout/error → rule-parser result + `degraded: ["llm_parse"]`; feed row errors → `feed_row_errors`.
6. No query text in telemetry by default (PII). Use `souq_common.telemetry.hash_query()`.
7. LLM calls are recorded/replayed in tests (`SOUQ_LLM_MODE=replay`, fixtures in `tests/fixtures/llm/`).
   Live LLM tests carry the `live` pytest marker / `@live` Jest tag and never run in default CI.

## Layout

```
apps/gateway          Fastify 5, REST /v1 + GraphQL Yoga /graphql, Redis response cache, auth, page tokens
apps/storefront       Next.js App Router, RTL (dir="rtl" for ar), GraphQL client
services/search       FastAPI: normalise → parse (rule|llm|cache) → bm25 ∥ knn → RRF → rerank → response
services/indexer      confluent-kafka consumer → OpenSearch bulk (AIMD backpressure, retry topics, DLQ, DLQ admin CLI)
services/feeds        FastAPI upload + streaming import → Postgres + outbox (same transaction)
services/outbox-relay Python. Polls outbox (FOR UPDATE SKIP LOCKED) → Redpanda catalog.changes
packages/contracts    openapi.yaml (public), schema.graphql, generated internal search OpenAPI, codegen, drift check
packages/db           node-pg-migrate migrations (expand/contract)
libs/py-common        souq_common: settings, budgets, telemetry, arabic (normalisation), embeddings
libs/admin-sdk        openapi-python-client generated admin SDK
evals/                golden set, filter-parse cases, harness, thresholds.yaml
models/               ONNX export scripts (reranker, embedder); artefacts gitignored
scripts/              reindex.py, seeds (ESCI, translation, synthetic feed) — uv member `souq-scripts`
infra/                compose.yaml + per-service config
config/               budgets.yaml (latency budgets), pricing.yaml (LLM price sheet)
tests/                cross-service integration (testcontainers), chaos, shared fixtures
loadtest/             k6 scripts
ci/                   CI helper scripts; .github/workflows/
```

Package names — Python: `souq-common` (import `souq_common`), `souq-search` (`souq_search`),
`souq-indexer` (`souq_indexer`), `souq-feeds` (`souq_feeds`), `souq-outbox-relay` (`souq_outbox_relay`),
`souq-scripts`, `souq-evals` (`souq_evals`), `souq-models`, `souq-admin-sdk`. Python 3.12, `src/` layout.
TS: `@souq/gateway`, `@souq/storefront`, `@souq/contracts`, `@souq/db`. Node 24, ESM, strict TS.

## Local stack (infra/compose.yaml, project name `souq`)

Host ports avoid 5432/8080/9090 (occupied on the dev machine by another stack). Docker VM has ~8 GB shared
with other containers: keep heaps small, never run more than one testcontainers OpenSearch at a time.

| Service | Host port | Notes |
|---|---|---|
| postgres 16 | **55432** | db/user/pass `souq`; extensions `ltree`, `pg_trgm` |
| redis-stack | 6379 | RediSearch for the semantic cache (vector index) + gateway response cache |
| redpanda | 19092 (kafka), 18081 (schema reg), 9644 (admin) | internal listener `redpanda:9092` |
| redpanda console | 8088 | profile `tools` |
| opensearch 2.x | 9200 | security plugin disabled for dev |
| opensearch-dashboards | 5601 | profile `tools` |
| otel-collector | 4317 grpc / 4318 http, 8889 prom exporter | profile `obs` |
| tempo 3200, prometheus **9091**, grafana 3001 | | profile `obs` |
| storefront 3000, gateway 4000, search 8001, feeds 8002 | | profile `apps` |

Env vars (see `.env.example`): `DATABASE_URL`, `REDIS_URL`, `KAFKA_BOOTSTRAP`, `OPENSEARCH_URL`, `SEARCH_URL`,
`FEEDS_URL`, `PAGE_TOKEN_SECRET`, `GATEWAY_API_KEYS`, `ANTHROPIC_API_KEY`, `SOUQ_LLM_MODE` (replay|record|live),
`SOUQ_EMBEDDER` (bge-m3|fake), `SOUQ_RERANKER_MODEL` (path to ONNX dir), `OTEL_EXPORTER_OTLP_ENDPOINT`,
`OTEL_SDK_DISABLED`, `SOUQ_CONFIG_DIR` (defaults to repo `config/`).

## Shared contracts

### Kafka (Redpanda)
Topics: `catalog.changes` (12 partitions), `catalog.retry.30s` (12), `catalog.retry.5m` (12), `catalog.dlq` (3).
**Key = sku** everywhere (per-SKU ordering). Producer: `enable.idempotence=true`, `acks=all`,
`max.in.flight.requests.per.connection=5`. Headers: `traceparent`, `tracestate`, `event_id`, `sku`, `version`,
`attempt` (int), `not_before` (epoch ms, retry topics), `original_topic`, `error_class`, `error_message`.

Value (JSON, UTF-8):
```json
{"event_id": "123", "event_type": "product.upserted|product.deleted", "sku": "B000123", "version": 7,
 "outbox_created_at": "2026-10-13T10:00:00.123Z", "product": { ...product_document(sku)... }}
```

### Postgres
`product_document(sku text) RETURNS jsonb` (SQL function, packages/db) builds the fat event payload:
`{sku, brand, category_path, category_levels[], price, currency, colour, size, attributes, title_en, title_ar,
description_en, description_ar, updated_at, version}`. Writers (feeds import) upsert `products` +
`product_texts` and insert `outbox(aggregate_id=sku, event_type, payload=product_document(sku), trace_context)`
in the **same transaction**; `products.version` increments on every effective change.
`outbox.trace_context jsonb` holds the W3C carrier so traces link feeds → relay → indexer.

### Supplier feed row (CSV header / JSONL keys)
`sku, title_en, title_ar, description_en, description_ar, brand, category_path ("Electronics > Audio > Headphones"),
price, currency, colour, size, attributes (JSON object string in CSV)`. Required: sku, title_en|title_ar, brand,
category_path, price, currency. Reject codes: `missing_field`, `invalid_price`, `invalid_currency`,
`invalid_json`, `invalid_category`, `too_long`, `duplicate_sku_in_feed`, `malformed_row`.

### OpenSearch
Indices `products_v1`, `products_v2`; aliases `products_read` (search reads) and `products_write`.
**Dual-write:** the indexer resolves `products_write` to its concrete indices (refresh every 10 s) and bulks to
**every** index in it; `scripts/reindex.py` adds the new index to `products_write`, backfills, verifies,
then swaps `products_read` atomically (`_aliases` actions). Rollback = swap back.
Indexing uses `version_type=external` with `version = products.version`; a 409 version conflict is success.
Fields: `sku` kw; `brand` text + `.kw`; `category_path` kw; `category_levels` kw[]; `price` scaled_float(100);
`currency` kw; `colour` kw (canonical English lowercase); `size` kw; `title_en`/`description_en` (english);
`title_ar`/`description_ar` (arabic + `souq_arabic_norm` char filter); `.raw` keyword subfields on titles;
`attributes` flat_object; `embedding` knn_vector **dim 1024 (bge-m3 dense)**, hnsw, lucene, cosinesimil;
`updated_at` date; `version` long. v2 adds `title_en.prefix` / `title_ar.prefix` edge-ngram subfields.

### Internal search API (services/search, FastAPI → `packages/contracts/search-internal/openapi.json`)
`POST /search`
```
req:  {q, lang: "en"|"ar"|"auto", filters?: Filters, cursor?: Cursor, limit: 1..50 (default 24),
       options?: {llm?: bool=true, rerank?: bool=true, debug?: bool=false}}
Filters: {price_min?, price_max?, currency?, brands?: [str], colours?: [str], sizes?: [str], category?: str}
Cursor: {query_key: str, pit_id: str, window_pos: int, search_after?: [any]}   # opaque to the gateway
resp: {items: [{sku, title, description, brand, price: {amount, currency}, category_path, colour, size,
                score, rank_source: "rerank"|"fused"|"lexical_tail"}],
       facets: [{name, buckets: [{value, count}]}], applied_filters: Filters,
       parsed_query: {original, normalised, detected_lang, script: "ar"|"en"|"mixed"|"arabizi", rewritten?,
                      filters: Filters, parser: "rule"|"llm"|"cache", unparsed_tokens: [str]},
       degraded: ["rerank"|"llm_parse"|"knn"|"bm25"|"semantic_cache"], next_cursor?: Cursor,
       total_estimate: int, timings_ms: {stage: float}}
```
`GET /healthz`, `GET /readyz`. Item `title`/`description` are localized by `lang` (fallback to the other language).

### Feeds internal API (services/feeds)
`POST /feeds` multipart(file, format=csv|jsonl) → 202 `{id, status}`; `GET /feeds/{id}` →
`{id, status: queued|running|completed|failed, rows_total, rows_ok, rows_rejected, error_counts: {code: n},
started_at, finished_at, failure_reason}`; `GET /feeds/{id}/errors?cursor&limit` → `{items: [{row_number, code,
field, message, raw}], next_cursor}`.

### Public page token (gateway)
`base64url(JSON{c: Cursor, qh: sha256(q|lang|filters canonical), exp}) + "." + base64url(HMAC-SHA256(secret, body))`.
Tampered, expired or query-mismatched tokens → 400 problem+json `type: .../invalid-page-token`.

### Python interfaces other packages code against (signatures are contracts)
```python
# souq_common.arabic  (owner: search agent)
normalise_arabic(text: str) -> str          # strip tashkeel U+064B–U+0652, U+0670, tatweel U+0640;
                                            # أ إ آ ٱ → ا ; ى → ي ; ة → ه ; ؤ → و ; ئ → ي ; Arabic-Indic digits → ASCII
arabizi_to_arabic(text: str) -> str         # 2→ء 3→ع 5→خ 7→ح (+ 3'→غ, 6→ط, 9→ق) + latin letter map
detect_script(text: str) -> Literal["ar","en","mixed","arabizi"]
normalise_query(text: str) -> NormalisedQuery   # dataclass(text, script, tokens: list[str], arabizi_ar: str|None)
# souq_common.embeddings  (owner: search agent)
get_embedder() -> Embedder   # Protocol: dim:int; encode_queries(list[str]) -> np.ndarray; encode_documents(list[str]) -> np.ndarray
document_text(doc: dict) -> str
# souq_common.telemetry  (owner: instrumentation agent)
init_telemetry(service_name: str) -> None
stage(name: str, budget_ms: float | None = None, **attrs) -> ContextManager[Span]   # span "stage.<name>"
metrics() -> SouqMetrics          # histograms/counters/gauges listed below
hash_query(q: str) -> str
inject_kafka_headers(headers: list[tuple[str, bytes]]) -> list[tuple[str, bytes]]
extract_kafka_context(headers) -> Context ; inject_carrier() -> dict ; context_from_carrier(dict) -> Context
instrument_fastapi(app) -> None   # call right after creating the FastAPI app (search, feeds)
llm_span(model: str, operation: str = "chat") -> ContextManager[Span]   # gen_ai.* attrs, no prompt text
record_llm_usage(span, input_tokens: int, output_tokens: int, cost_usd: float | None = None) -> None
# souq_common.budgets
load_budgets() -> Budgets         # attribute access, e.g. load_budgets().search.rerank_ms
# souq_search.fusion
rrf(rankings: Sequence[Sequence[str]], k: int = 60, weights: Sequence[float] | None = None) -> list[tuple[str, float]]
# souq_search.parsing.rules
parse_rules(text: str, lang: str) -> RuleParse   # .filters: Filters, .residual: str, .unparsed_tokens, .constraint_like: list[str]
# souq_indexer.aimd
AimdController(initial=1000, min_size=50, max_size=5000, additive_step=100, decrease_factor=0.5)
  .batch_size: int ; .on_success() ; .on_rejection() ; .backoff_seconds(consecutive_rejections) -> float
# souq_indexer.routing
classify_item_error(status: int, error_type: str | None) -> Literal["ok","conflict_ok","retryable","non_retryable"]
next_route(attempt: int, retryable: bool, now_ms: int) -> Route   # Route(topic, not_before_ms, attempt)
```
TS: `apps/gateway/src/pagination/page-token.ts` exports `encodePageToken`, `decodePageToken`,
`InvalidPageTokenError`; `apps/gateway/src/cache/keys.ts` exports `searchCacheKey(input)`;
`apps/gateway/src/graphql/resolvers.ts` exports `resolvers` using `ctx.searchClient`.

### Telemetry names
Spans: one trace per search; gateway span → `POST /search` → children `stage.normalise`, `stage.parse`
(attr `souq.parse.path` = rule|llm|cache_hit), `stage.embed`, `stage.bm25`, `stage.knn`, `stage.fuse`, `stage.rerank`.
Stage attrs: `souq.stage.budget_ms`, `souq.stage.exceeded` (bool). LLM spans use `gen_ai.*` semconv incl. token usage.
Metrics: `search_latency` (hist, ms, attr `path`=standard|llm|cache), `stage_latency` (hist, attr `stage`),
`degraded_total` (attr `reason`), `zero_result_total`, `llm_path_ratio`, `semantic_cache_hit_ratio`,
`consumer_lag` (attr `partition`), `bulk_rejections_total`, `index_freshness_seconds` (hist),
`dlq_depth`, `indexer_batch_size` (gauge, AIMD).

### LLM record/replay fixtures
`tests/fixtures/llm/<key>.json` where `key = sha256(model + "\n" + canonical_json(request_body))[:16]`;
file = `{"model", "request", "response", "usage": {"input_tokens", "output_tokens"}, "recorded_at"}`.
`SOUQ_LLM_MODE=replay` → read fixture, missing fixture raises (tests) / degrades (service); `record` → call API
and write; `live` → call API, no write.

### Eval report (`evals/reports/<run_id>.json`, consumed by CI)
```json
{"run_id": "...", "git_sha": "...", "mode": "offline|full",
 "slices": {"en": {...}, "ar": {...}, "arabizi_mixed": {...}, "all": {...}},   // per slice ↓
   // {"<ablation>": {"ndcg@10": 0.0, "recall@100": 0.0, "mrr": 0.0, "zero_result_rate": 0.0, "n": 60}}
   // ablations: bm25, knn, rrf, rrf_rerank, rrf_rerank_llm
 "filter_parse": {"exact_match": 0.0, "n": 80, "per_field": {"price": 0.0, "brand": 0.0, ...}},
 "latency_ms": {"<stage>": {"p50": 0.0, "p95": 0.0}},
 "llm": {"path_share": 0.0, "avg_tokens": 0.0, "usd_per_1k_queries": 0.0}}
```
`evals/thresholds.yaml` holds absolute targets; CI also compares against the main-branch report and blocks if
nDCG@10 (best production ablation) drops > 1.0 point on any language slice.

## Commands
```
pnpm install && uv sync --all-packages      # bootstrap
pnpm turbo build lint typecheck test         # everything (Python via uv)
uv run pytest -m "not integration and not live and not chaos"
uv run pytest -m integration                 # testcontainers
docker compose -p souq -f infra/compose.yaml up -d                  # core data services
docker compose -p souq -f infra/compose.yaml --profile obs --profile tools up -d
pnpm --filter @souq/db migrate up
```

## Conventions
- Python: ruff (lint+format), mypy --strict on `src/`, pytest; pydantic v2; httpx; structlog-style JSON logs.
- TS: ESLint flat config, `tsc --noEmit`, Jest (ts-jest ESM). Zod at boundaries.
- Errors at HTTP edges are RFC 9457 problem+json.
- Migrations are expand/contract; never drop or rename in the same release that adds.

## Compose files
`infra/compose.yaml` (core data services) `include`s `infra/compose.obs.yaml` (profile `obs`, observability-owned)
and `infra/compose.apps.yaml` (profile `apps`, built from each app/service `Dockerfile`).

## Parallel build coordination (initial build only)
The initial build was split across 8 agents. Ownership is by path — only the owner edits a file. If you need a
change in someone else's file, append a request to `docs/handoffs/<owner>.md`. Owners: see `docs/OWNERSHIP.md`.
Do not `git commit`. Do not run `docker compose down`, `docker rm`, or `docker system prune`.
Heavy Docker work (testcontainers suites, chaos tests, image builds, k6 runs) takes a lock first:
`until mkdir .locks/heavy-docker 2>/dev/null; do sleep 15; done` … then `rmdir .locks/heavy-docker` (always, even on failure).

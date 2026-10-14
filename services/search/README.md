# services/search

FastAPI on :8001. Owns ranking end to end: normalise → parse (rule | semantic cache | LLM) → BM25 ∥
(embed → kNN) → RRF → rerank → page. Contract: `POST /search`, `GET /healthz`, `GET /readyz` as in
CLAUDE.md; the generated spec lives in `packages/contracts/search-internal/openapi.json`
(`pnpm --filter @souq/search openapi`, then `pnpm --filter @souq/contracts codegen`; a test fails if the
committed spec is stale).

```
uv run --package souq-search souq-search                 # HOST/PORT (default 0.0.0.0:8001)
pnpm --filter @souq/search test                          # unit tests (fakes for OpenSearch/LLM/Redis)
uv run --package souq-search pytest services/search/tests -m integration   # Redis Stack + Postgres
```

## Stages and budgets

Every stage runs inside `souq_common.telemetry.stage(name, budget_ms)` and under `asyncio.wait_for` with
`min(stage budget, time left in the request)`. All numbers come from `config/budgets.yaml` (`search.*`);
the request budget is `total_standard_ms`, raised to `total_llm_ms` only when the LLM path is taken.

| stage | what | on timeout / error |
|---|---|---|
| `normalise` | `souq_common.arabic.normalise_query` (the only normaliser) | — |
| `parse` | rule parser; LLM only if it leaves constraint-like tokens | rule result + `degraded: ["llm_parse"]` |
| `semantic_cache` | Redis Stack KNN lookup before the LLM | skip cache + `degraded: ["semantic_cache"]` |
| `bm25` (∥) | term-centric multi-field BM25 + facet aggregations | kNN only + `degraded: ["bm25"]` |
| `embed` → `knn` (∥) | bge-m3 query vector, kNN with filters inside the kNN clause | BM25 only + `degraded: ["knn"]` |
| `fuse` | `souq_search.fusion.rrf` (k = `rrf_k`) | — |
| `rerank` | ONNX int8 cross-encoder over the top `rerank_window` | fused order + `degraded: ["rerank"]` |

The reranker runs on a dedicated thread with one slot (`SOUQ_RERANK_SLOTS`): `wait_for` cannot stop a
running inference, so a timed-out one keeps its slot until it finishes and requests arriving meanwhile
degrade immediately instead of queueing. `search.rerank_mode` chooses how its scores are used: `replace`
(sort the window by cross-encoder score) or `rrf` (the cross-encoder's order *votes* with the fused order,
weight `rerank_rrf_weight`) — see `evals/README.md` for why the default is what it is.

Both retrievers failing is the only hard failure (503 problem+json). Errors are RFC 9457
(`https://souq.dev/problems/{validation-error,invalid-cursor,search-unavailable}`).

## Query understanding

* **Rules** (`souq_search.parsing.rules.parse_rules`): price operators and ranges in English, Arabic and
  Arabizi (`under/below/less than`, `اقل من/تحت/دون`, `ta7t/a2al men`, `between/بين/من…الى`, `300 aed or less`,
  `$50`, `1,500`, `2k`), currencies (AED/درهم/dhs, SAR, USD, …), brands (static seed plus the catalog's brands,
  minus everyday words), canonical English colours (Arabic masculine/feminine/ال forms, Arabizi), sizes
  (`size 42`, `مقاس كبير`, `xl`, `eu 43`). Numbers followed by units (`55 inch`, `3 years`) are not prices.
* **LLM** (`souq_search.parsing.llm`): only when the leftovers look like constraints the rules could not read —
  price words (`cheap`, `رخيص`), approximations (`around`, `بحدود`), numbers in words (`two hundred`, `ميتين`),
  a currency without an amount. One Messages API call to `SOUQ_LLM_MODEL` (default `claude-haiku-5-5`) with a
  strict JSON schema (`output_config.format`), thinking disabled, effort `low`, `max_tokens` =
  `llm_max_output_tokens`; the prompt is ~500 tokens, well under `llm_tokens_per_query` (1.5k, enforced before
  sending). The SDK runs with `max_retries=0` and the stage budget as its timeout. Spans via `llm_span` /
  `record_llm_usage`; cost from `config/pricing.yaml`.
* **Semantic cache** (`souq_search.parsing.semantic_cache`): RediSearch HNSW index (`souq_llm_cache`, cosine,
  1024-d) over the normalised-query embedding; a hit needs similarity ≥ `semantic_cache_similarity` (0.95) and
  an exact match on language, prompt version and the query's *number signature* — otherwise "under 200" and
  "under 300" (cosine ≈ 0.99) would share a cached price. TTL `semantic_cache_ttl_s`.
* **Hard vs soft filters**: request filters (facet clicks) are always hard. A parsed brand/colour/size is a
  hard filter only if that field is populated on ≥ 80 % of the index (coverage refreshed every 10 min);
  otherwise it boosts matching products in BM25. On the ESCI seed size is on 16 % of products, so "size 42"
  as a hard filter would hide most relevant results. If parsed filters still produce zero hits, the query is
  retried once with only the request's filters and the full text.

## Record / replay (`SOUQ_LLM_MODE`)

Fixtures: `tests/fixtures/llm/<key>.json`, `key = sha256(model + "\n" + canonical_json(request_body))[:16]`
(`SOUQ_LLM_FIXTURES_DIR` overrides the directory). `replay` (default) never calls the API — a missing fixture
degrades to the rule parse (and raises in tests); `record` calls and writes; `live` calls without writing.
No API key exists in this environment, so the committed fixtures are hand-authored answers
(`tests/fixtures/llm/_authoring/synthetic.yaml` → `scripts/make_synthetic_fixtures.py`) marked
`"recorded_at": "synthetic"`. To replace them with real recordings:
`SOUQ_LLM_MODE=record ANTHROPIC_API_KEY=… uv run --package souq-evals python -m souq_evals --full`.
`test_synthetic_fixtures.py` fails if a prompt/schema change invalidates the committed keys.

## Pagination

Goal: page N+1 never repeats or skips anything from pages ≤ N, even though reranking is not reproducible
across calls (latency-dependent degradation, catalog updates between requests).

1. **Page 1** computes the fused pool (BM25 ∪ kNN, ≤ 2 × `candidates_per_retriever`) and the reranked window
   (top `rerank_window`). If there may be a next page it stores a *pagination session* in Redis under
   `souq:pg:<query_key>`, `query_key = <16-hex query hash>.<8-hex nonce>`: the window's SKU order and scores,
   the rest of the fused order, facets, parsed query, applied filters and the retrieval terms. TTL
   `pagination_ttl_s` (= `pit_keep_alive`), extended on every page read. ~5 KB per session.
2. **Cursor** `{query_key, pit_id, window_pos, search_after?}` — `window_pos` is the absolute position of the
   next item. Pages inside the pool are served from the frozen order (`rank_source` `rerank` inside the window
   — or `fused` if page 1's rerank degraded — then `fused`), fetching documents by SKU in one `terms` query;
   a product deleted since page 1 is skipped without shifting later positions.
3. **Lexical tail**: when a page runs past the pool and BM25 matched more than it returned, the service opens
   an OpenSearch **point in time** (lazily — opening one per first page would exhaust OpenSearch's open-PIT
   limit under load) and continues with the same BM25 query, `must_not` the pool's SKUs, sorted by
   `(_score desc, sku asc)` (a total order) with `search_after`. Those items are `rank_source: "lexical_tail"`;
   the cursor then carries the real `pit_id` and the last sort values. An expired PIT is reopened and the
   `(score, sku)` sort values are reused.
4. A cursor whose query hash does not match the request is a 400 (`invalid-cursor`); an expired session is
   recomputed from scratch (best effort, logged).

## query_log

`QueryLogWriter` batches rows (`query_log_batch`, every `query_log_flush_ms`) from a bounded in-memory queue
into `query_log` with multi-row INSERTs on a background task; a full queue drops rows (counted) rather than
blocking a request. `query_hash = souq_common.telemetry.hash_query(normalised)` — set `SOUQ_TELEMETRY_SALT`
in deployments so hashes group across processes. `SOUQ_QUERY_LOG=false` disables it,
`SOUQ_QUERY_LOG_TEXT=false` stores hashes only.

## Configuration

| env | default | |
|---|---|---|
| `SOUQ_EMBEDDER` / `SOUQ_EMBEDDER_MODEL` | `fake` / `models/artifacts/bge-m3-int8` | `bge-m3` in real deployments |
| `SOUQ_RERANKER_MODEL` | `models/artifacts/reranker-int8` | missing → every response `degraded: ["rerank"]` |
| `SOUQ_ORT_THREADS` | 4 | intra-op threads per ONNX session |
| `SOUQ_RERANK_SLOTS` | 1 | concurrent rerank inferences; busy → degrade immediately |
| `SOUQ_LLM_MODE` / `SOUQ_LLM_MODEL` | `replay` / `claude-haiku-5-5` | |
| `SOUQ_SEARCH_INDEX` | `products_read` | alias searched |
| `SOUQ_QUERY_LOG` / `SOUQ_QUERY_LOG_TEXT` | `true` / `true` | |

Model choice, latency and quality numbers: `models/README.md`. Relevance numbers: `evals/README.md`.

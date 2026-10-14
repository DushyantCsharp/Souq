# What breaks first at 10×

The 1× design point is **200 RPS** of search (the latency target in the README) and a catalog the size of
the synthetic supplier feed, **500k SKUs**. 10× is **2,000 RPS** and **5M SKUs**. Everything below is
ranked by how early it breaks, using measurements from one laptop. Numbers marked *derived* are
arithmetic on measured numbers, with the formula shown. Numbers marked *not measured* were not measured.

## Where the measurements come from

- One Apple M-series laptop, 10 cores / 16 GB. The Docker VM has 10 vCPU / 7.7 GB and is shared by the whole
  stack and the load generator; an unrelated container stack shared it during the early runs and was
  stopped before the final ones (`docs/perf-report.md`, "Run conditions").
- `services/search`: 2 uvicorn workers × 2 ORT threads, 4 GB limit. Index `products_v2`, **51,001 docs**,
  bge-m3 int8 embeddings, mMiniLM-L12 int8 reranker.
- OpenSearch: one node, 768 MB heap (`-Xms768m -Xmx768m`), templates with 1 shard and 0 replicas.
- Redis Stack: `--maxmemory 256mb --maxmemory-policy allkeys-lru` (`infra/compose.yaml`).
- Local numbers are a pessimistic lower bound on capacity, not a production SLO measurement.

## Summary

| Rank | Component | Breaks at | Key evidence | Fix |
|---|---|---|---|---|
| 1 | Search-service CPU (query embedding + cross-encoder) | **below 1×**: ~60 RPS per node | 200 RPS → 99.9 % errors before admission control; ≈ 180 ms CPU per request | admission control; replicas; embedding cache; drop the CPU reranker or move it and bge-m3 to GPU |
| 2 | LLM parse path | **today, at any load**: live latency exceeds the budget | live Claude Haiku 5.5 steady state p50 1.79 s / p95 2.92 s (40 calls; 0 % within the 800 ms `parse_llm` budget); 1.21 s p50 without strict JSON-schema decoding | keep the path ≤ 10 %; fill the semantic cache off the request path; raise rate limits before 10× |
| 3 | OpenSearch, one node | 10× QPS **and** 5M vectors | ~13.5 ms query + 9.4 ms fetch per search; young-GC pauses ~38 ms; 5M × 1024-d float32 ≈ 20 GB of vectors (derived) | shards + replicas, heap and off-heap RAM sized for vectors, more nodes |
| 4 | Re-embedding (reindex, embedder change, bulk catalog load) | 5M SKUs | 30–44 docs/s per 4 threads → 31.6–46.3 h per full re-embed (derived) | embedding cache by content hash; more indexer consumers; GPU batch embedding |
| 5 | Feed import | 5M-row feeds | 480 rows/s → ~2.9 h per 5M-row feed (derived); per-row trigger is 56 % of wall time | statement-level version trigger; set-based `product_documents(skus[])` |
| 6 | `catalog.changes` partition count | when #4 needs more than 12 consumers | 12 partitions = at most 12 active consumers per group | raise partitions in a planned change (safe because of external versioning) |

## 1. Search-service CPU: already broken at 1×

**Evidence.**

- Direct probe of `services/search` (head queries, no gateway): 60 RPS sustained at p95 144 ms with
  2.5 % 503s. At 30 RPS the service used 280 % CPU, at 60 RPS 305 %.
- At 200 RPS it collapsed before admission control: 99.94 % errors, 152 of 200 req/s achieved, both
  retrievers timing out on almost every request (`docs/perf-report.md`).
- Search CPU was ≈ 180 ms per request before the fixes in the perf report, mostly from two models running
  in-process on CPU:
  - **bge-m3 query embedding** (int8): ~20 ms wall, ~40–60 ms CPU per query. The direct probe hid this
    because its small query pool hit the in-process LRU cache (embed p50 ~0.1 ms).
  - **Cross-encoder over the top 30**: mMiniLM int8 72 ms p50 / 105 ms p95 on a quiet CPU, and
    194 / 562 ms at load average 28 (`models/README.md`). The better bge-reranker-v2-m3 int8 is 537 ms
    p50 on a quiet CPU. Under load the circuit breaker sheds reranking almost entirely: 3 % of requests
    were reranked at 60 RPS.

**Derived.** 200 RPS × 180 ms CPU = 36 cores of work against a 10-core laptop, which is consistent with the
collapse. At 10×: 2,000 × 180 ms = **360 cores**. Embedding alone at 2,000 RPS with no cache hits:
2,000 × 40–60 ms = **80–120 cores**, scaled by (1 − embedding-cache hit rate). At the observed ~40–60 RPS
per laptop-class node, 2,000 RPS needs **~34–50 search nodes** (2,000 ÷ 60 to 2,000 ÷ 40). The perf
report's own estimate for 1× is ~4 replicas.

**Fixes, in order.**

1. **Admission control** (`search.max_inflight_per_worker`, 503 + `Retry-After`; ADR 0003). It doesn't add
   capacity, but overload then serves about capacity instead of nearly nothing. Effect: pending
   re-measurement.
2. **Drop the CPU reranker or move it to a GPU.** The evals show mMiniLM does not beat RRF (all slices
   nDCG@10 0.4662 `rrf` vs 0.4579 `rrf_rerank`, `evals/reports/final-offline.json`), and at load it is shed
   anyway. On CPU it costs budget for no measured gain. bge-reranker-v2-m3 is ~2 nDCG points better
   (RRF 0.466 → 0.487 in the eval harness), but only viable on a GPU.
3. **Embedding cache hit rate.** Query embeddings are LRU-cached per process (4,096 entries). A shared
   cache (Redis, keyed on the normalised query) lets head queries skip bge-m3 across replicas. The
   production hit rate is not measured. Measure it before sizing, because it scales the 80–120 cores
   directly.
4. **Replicas, or a GPU for bge-m3.** Horizontal scale works because the service is stateless (pagination
   sessions are in Redis). A GPU inference server for bge-m3 (and the reranker, if kept) removes the
   per-request CPU cost from the search pods. GPU latency and throughput: not measured.

## 2. LLM parse path: over budget at any load

**Evidence.** Live Claude Haiku 5.5 parses, 40 long-tail queries after a warm-up call, sequential:
p50 1,790 ms, p95 2,918 ms, **0 % within the 800 ms `parse_llm` budget** (5 % under 1.2 s); 1,398 tokens per
query on average. Without strict JSON-schema decoding the same prompt ran at p50 1,214 ms with half the input
tokens (the schema is added to the prompt), still over budget. Part of the latency is the network path from
the test machine to the API region (not separated out). Load-test LLM numbers replay recorded fixtures, so
they do not include live model latency.

**What happens today.** A parse that misses the budget degrades to the rule parse (`llm_parse`). The
semantic cache is written only after a *successful* LLM parse (`pipeline.py`, `_parse_llm`). If the live
model is consistently slower than the budget, the cache never fills: every LLM-path query pays for a call
that gets abandoned and the shopper still gets the rule result.

**At 10× (derived).** With the target share of ≤ 10 % LLM-path traffic, 2,000 RPS means up to **200 LLM
calls/s**. At p50 1.79 s / p95 2.92 s each that is **≈ 360–580 calls in flight** (Little's law) and **12,000
requests/min**.
At the 1.5k-token cap that is up to 18M tokens/min (200 × 60 × 1,500). Our API rate limits for that volume:
not measured.

**Fixes.**

1. **Fill the cache off the request path.** Answer with the rule parse within budget, and let the LLM call
   finish in the background (bounded concurrency) and write the semantic cache, so the next paraphrase is
   a cache hit. Precompute parses for head queries in batch.
2. **Keep the path share low.** The rule parser and the cache should handle the common forms. Track
   `llm_path_ratio` and `semantic_cache_hit_ratio` as capacity metrics, not just cost metrics.
3. **Raise rate limits and set a client-side concurrency cap** before 10×, so a provider slowdown turns
   into fast degradation instead of hundreds of stuck connections.
4. **Re-check the budget** once steady-state latency is measured. If p95 cannot fit 800 ms, the synchronous
   LLM path cannot meet the 1.2 s LLM-path target, and fix 1 becomes the design.

## 3. OpenSearch: one node, 768 MB heap

**Evidence.** ~13.5 ms query + 9.4 ms fetch per search, young-GC pauses ~38 ms, on one node with a 768 MB
heap and 1 shard / 0 replicas. A 38 ms pause is close to half the 80 ms kNN budget.

**At 10× (derived).**

- *QPS.* Each user search issues a BM25 and a kNN search. 2,000 × 2 × (13.5 + 9.4) ms ≈ **92 s of
  query+fetch time per second**, so ~92 searches in flight across the cluster's search threads at all
  times, before the facet aggregations.
- *Vectors.* 5,000,000 docs × 1024 dims × 4 bytes ≈ **20.5 GB of raw float32 vectors** per copy of the
  index, before HNSW graph links (today: 51,001 × 4 KiB ≈ 209 MB). Lucene's HNSW search needs those
  vectors in the page cache to stay fast. A 7.7 GB VM cannot hold them, so kNN would read from disk.

**Fixes.** Shard the index so each shard's vectors fit in its node's page cache, and add replicas for QPS
(both are template settings, `number_of_shards` / `number_of_replicas`, applied through a zero-downtime
reindex, ADR 0002). Give nodes a larger heap for aggregations, and most of their RAM off-heap for vectors.
Consider byte-quantised vectors to cut vector memory. Retrieval quality impact: not measured. Re-measure
GC pauses under load against the 80 ms kNN budget.

## 4. Re-embedding at 5M SKUs

**Evidence.** Document embedding with bge-m3 int8: ~30–44 docs/s on 4 ORT threads (`models/README.md`).
8 threads stalled to ~2 docs/s on an oversubscribed host. A 500k-SKU re-embed takes ≈ 3–4.6 h.

**At 10× (derived).** 5,000,000 ÷ 44 ≈ 113,600 s ≈ **31.6 h**; 5,000,000 ÷ 30 ≈ 166,700 s ≈ **46.3 h** for
one 4-thread embedder. That is every zero-downtime reindex, every embedder change, and the first load of a
new catalog. During a reindex, live changes are dual-written and also embedded, so the indexer competes
with the backfill.

**Fixes.**

1. **Embedding cache keyed on the document text.** `reindex.py` already caches vectors on disk
   (`data/cache/embeddings-bge-m3.sqlite`), so a mapping-only reindex re-embeds nothing that hasn't
   changed. Make the indexer use the same content-hash cache, so live updates that don't touch text skip
   embedding.
2. **Scale out embedding.** Up to 12 indexer consumers (the partition count, see §6). Linear scaling would
   be ~2.6–3.9 h for 5M on twelve 4-thread workers. Not measured.
3. **GPU batch embedding** for backfills. Throughput: not measured.

## 5. Feed import at 5M rows

**Evidence.** The 500k-row feed imports at **480 rows/s** (1,042 s); an unchanged re-import runs at
2,547 rows/s. In a 20k-row profile, `INSERT INTO product_texts … ON CONFLICT` takes 56 % of wall time,
because the per-row `product_texts_touch_product` trigger issues two extra non-HOT `products` updates per
new SKU. The outbox insert with `product_document(sku)` takes 19 % (`docs/handoffs/data.md`). Measured on
a shared Postgres at host load average ~20.

**At 10× (derived).** 5,000,000 ÷ 480 ≈ 10,400 s ≈ **2.9 h** per full 5M-row feed (unchanged
re-import: 5,000,000 ÷ 2,547 ≈ 33 min).

**Fixes.** A **statement-level** trigger (`REFERENCING NEW TABLE … FOR EACH STATEMENT`) that bumps each
product's version once per batch with one set-based `UPDATE`. Then an inlinable or set-returning
`product_documents(skus text[])`, so the outbox insert is one call per batch instead of one per row.
Improvement: not measured.

## 6. Partition count

`catalog.changes` has 12 partitions (and 12 on each retry topic), so one consumer group has at most 12
active indexers. That is the ceiling on fix 4.2. Raising it remaps sku → partition. Ordering across the
change is protected by external versioning (ADR 0001), but it should still be a planned change: add
partitions, then scale consumers. Throughput per partition: not measured.

## Not measured, likely to matter at 10×

- **Redis memory.** Pagination sessions are ~5 KB each with a 300 s TTL (`services/search/README.md`,
  `search.pagination_ttl_s`). If every first page stored one, 2,000 RPS × 300 s × 5 KB ≈ 3 GB (derived
  upper bound), and even 1× (≈ 300 MB) exceeds the dev instance's 256 MB. With `allkeys-lru`, evicted
  sessions are recomputed on the next page (more search CPU), and the semantic cache and the gateway
  response cache are evicted from the same instance.
- **Outbox relay throughput** (500 rows per poll, 100 ms poll interval, `config/budgets.yaml`): ceiling
  not measured.
- **Gateway** at 2,000 RPS: not measured. In cold-cache k6 runs the response-cache hit rate was 3.3 % by
  design (`CACHE_MODE=cold`). A warm-cache hit rate for real traffic is not measured.
- **The 60 RPS gap** between k6 through the gateway (p95 568 ms, 36 % errors) and the direct probe
  (p95 144 ms) is not explained yet (`docs/perf-report.md`).

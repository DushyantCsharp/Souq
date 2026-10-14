# ADR 0003: Per-stage latency budgets and a degrade-don't-fail policy

- Status: Accepted (admission control: being added, see below)
- Date: 2026-10-14
- Scope: `config/budgets.yaml`, `services/search` (`pipeline.py`, `rerank.py`, `admission.py`),
  `services/feeds`, the gateway and storefront timeouts.

## Context

A search request passes through stages with very different costs and failure modes. Normalising and rule
parsing take well under a millisecond. Query embedding (bge-m3 int8, ~20 ms wall) and the cross-encoder
(mMiniLM int8, top-30 72 ms p50 / 105 ms p95 on a quiet CPU) are in-process CPU inference. BM25 and kNN
are network calls to OpenSearch. The LLM parser is an external API call whose latency we don't control.
Targets: p95 ≤ 250 ms on the standard path and ≤ 1.2 s on the LLM path.

Each stage after retrieval makes results somewhat better; none is required for a usable page. A shopper who
gets fused-order results, or results without the LLM's reading of "around 200 dirhams", is better off than
one who gets an error page. The policy also has to hold under overload, which is when stages start missing
their budgets together.

## Decision

### 1. One budget file, enforced with timeouts

Every latency budget lives in `config/budgets.yaml`. Python reads it via `souq_common.budgets.load_budgets()`
and TypeScript via `apps/gateway/src/config/budgets.ts`. No budget literal appears in code (CLAUDE.md
rule 3). Current values (ms):

| Stage | Budget | Notes |
|---|---|---|
| request, standard path | 250 | `total_standard_ms` |
| request, LLM path | 1200 | `total_llm_ms`; the request deadline is raised to this only when the LLM path is taken |
| normalise / parse_rule / fuse | 5 / 5 / 5 | |
| semantic_cache | 15 | Redis Stack vector lookup before the LLM |
| parse_llm | 800 | |
| embed → knn | 50 → 80 | sequential; BM25 runs concurrently with both |
| bm25 | 120 | |
| rerank | 120 | top `rerank_window` = 30 |
| gateway → search | 1400 | > `total_llm_ms` + slack |
| storefront → gateway | 1600 | > gateway timeout + slack |

Each stage runs inside `stage(name, budget_ms)` (span `stage.<name>` with `souq.stage.budget_ms` and
`souq.stage.exceeded`) and under `asyncio.wait_for` with `min(stage budget, time left in the request)`.
OpenSearch calls carry the stage budget as their own httpx timeout, and `wait_for` is a 50 ms-later
backstop, so the HTTP client times out first and releases its pooled connection cleanly (see Evidence,
fix 5). The embed/bm25/knn budgets were re-split once after load tests (from 30/90/60). The 250 ms total
did not change.

### 2. Degrade, don't fail

| Failure | Response | `degraded` |
|---|---|---|
| LLM parse times out, errors, or (in replay mode) has no fixture | rule-parser result | `llm_parse` |
| Semantic cache unavailable or slow | skip the cache, go straight to the LLM | `semantic_cache` |
| BM25 times out or errors | kNN results only | `bm25` |
| Embed or kNN times out or errors | BM25 results only | `knn` |
| Reranker missing, busy, breaker open, or over budget | fused (RRF) order for the window | `rerank` |
| BM25 **and** kNN both fail | **503** problem+json (`search-unavailable`), the only hard failure | — |
| Feed row invalid (`invalid_price`, `duplicate_sku_in_feed`, …) | row recorded in `feed_row_errors` with code, field and raw row; the import continues | `rows_rejected`, `error_counts` on the feed job |

The response always says what was skipped. The storefront shows a notice and still renders the results.
`degraded_total{reason}` counts every degraded response. The gateway caches responses with a non-empty
`degraded` for only `degraded_response_cache_ttl_s` (5 s), so a degraded page does not stay cached for the
full 60 s TTL. On later pages, a pagination session whose page 1 was degraded carries its reasons forward,
so the ordering of the whole result list is reported consistently.

### 3. Keep a struggling stage from taking the rest down

- **Reranker slot and circuit breaker** (`souq_search.rerank.Reranker`). Inference runs on a dedicated
  thread with one slot (`SOUQ_RERANK_SLOTS`). `wait_for` cannot stop a running inference, so a timed-out
  one keeps its slot until it finishes, and requests that find the slot busy degrade immediately instead of
  queueing. The breaker records whether each attempt finished within its deadline over a window of 20
  attempts. When ≥ 50 % were late it sheds reranking for 5 s, then clears the window and judges the
  reranker on fresh attempts only.
- **Embedding work is cancelled, not abandoned.** `EmbedService` cancels queued embeds whose request has
  already given up, instead of running them later. ORT intra-op thread spinning is off (`SOUQ_ORT_SPIN=0`).
- **Admission control** (`souq_search.admission.AdmissionControl`, being added). A bounded number of
  in-flight `/search` requests per uvicorn worker (`search.max_inflight_per_worker`, currently 8, sized
  by Little's law from ~30 RPS per worker × 0.25 s). Requests beyond it get an immediate 503
  problem+json (`search-overloaded`) with `Retry-After: search.shed_retry_after_s` (1 s), counted as
  `degraded_total{reason="shed"}`. This is the one deliberate exception to "degrade, don't fail".
  Above capacity, refusing some requests quickly is what keeps the admitted ones inside their budget.
  **Measured effect: pending re-measurement.**

## Alternatives considered

| Option | Why not |
|---|---|
| Fail the request when any stage misses its budget | A slow reranker or LLM provider becomes a search outage. |
| One overall request timeout, no per-stage budgets | A slow early stage uses up the time of later ones, and nothing records which stage overran. |
| Queue rerank requests until a slot frees | Under load the queue only grows. Measured: the reranker used ~350 % of 520 % CPU at 30 RPS producing scores that were discarded, starving embedding and retrieval. |
| Retry inside the request (LLM, OpenSearch) | Doubles tail latency exactly when the dependency is slow. The LLM client runs with `max_retries=0` and the stage budget as its timeout. |
| Hedged requests to OpenSearch | Adds load during the overload that caused the slowness. |
| Rely on the gateway's per-client rate limit (120 req/min per IP) | Protects against one noisy client, not against aggregate load above capacity. |

## Consequences

- **Quality depends on load, visibly.** Under pressure the reranker is shed first and responses report
  `degraded: ["rerank"]`. In the current evals the mMiniLM reranker does not beat plain RRF
  (`evals/reports/final-offline.json`, all slices: `rrf` 0.4662 vs `rrf_rerank` 0.4579 nDCG@10), so
  shedding it costs latency budget, not measured quality.
- **The budgets are tuning parameters.** They were re-split once from measurements and will move again.
  Because they live in one file, changing them is a reviewed config change, not a code hunt.
- **Clients have to handle 503 + `Retry-After`** once admission control ships. Shed requests are fast,
  so the tail of successful requests shortens, but the error rate under overload is non-zero by design.
- **Tests cover the fallbacks.** `tests/integration/test_rerank_degrade.py` checks that a slow reranker
  degrades within budget, a busy one degrades immediately, a missing one degrades, and a healthy one is not
  degraded.

## Evidence

From [docs/perf-report.md](../perf-report.md), "Bottlenecks found while measuring" (all fixed):

| # | Found | Fix | Measured |
|---|---|---|---|
| 1 | A fresh search process missed the retrieval budgets on its first requests (cold start → 503) | `souq_search.app.warm_up` runs the embedder, reranker and OpenSearch client before `/readyz` passes | — |
| 2 | ORT's CPU arena grew to the largest input shape and never shrank; workers OOM-killed | arena and memory-pattern planning off for both sessions | 114 restarts in 10 min with 3 workers under 3.5–4 GB → 0 restarts with 2 workers × 2 threads |
| 3 | Timed-out rerank inferences kept running | circuit breaker (20-attempt window, ≥ 50 % late → shed 5 s) | ~350 % of 520 % CPU used by the reranker at 30 RPS before the fix |
| 4 | Abandoned embeds piled up | `EmbedService` cancels work that has not started; `SOUQ_ORT_SPIN=0` | — |
| 5 | Cancelling httpx requests mid-flight leaked pooled connections | OpenSearch calls shielded from caller cancellation and bounded by their own timeout | after a 5-minute overload all 200 connections were stuck (`PoolTimeout`) until restart; now the service recovers within ~20 s of the load stopping |

What the policy does under load today (same report):

- Direct probe of `services/search`, 60 RPS, head queries: p95 144 ms, 97.5 % 200 OK, kNN ran on 97 % of
  requests, reranked 3 %.
- k6 full mix at 60 RPS through the gateway: degraded 99.98 % (rerank shed by the breaker), 36.3 % errors.
- k6 at 200 RPS **before admission control**: 99.94 % errors, 100 % degraded. Both retrievers timed out on
  almost every request because offered load was ~3× capacity and nothing shed it (congestion collapse).
- Reranker under contention (`models/README.md`): mMiniLM int8 top-30 at 194 / 562 ms p50/p95 at load
  average 28, vs 72 / 105 ms quiet.
- Admission control at 60 and 200 RPS: **pending re-measurement**.

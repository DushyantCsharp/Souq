# Souq — performance report

Status: **measured 2026-10-14 — the standard-path SLO is not met on a single laptop.** Every number below
comes from a k6 run (`loadtest/results/`), the direct search probe, or Prometheus; nothing is estimated.
The overload robustness bugs the runs exposed are fixed (see "Bottlenecks found"); raw capacity is not.

## Targets

| Target | Value | Measured | Status |
|---|---|---|---|
| Standard path p95 at the gateway, 200 RPS | ≤ 250 ms | 4,615 ms; 99.9 % errors (overload collapse) | **miss** |
| Standard path p95 at the gateway, 60 RPS (full k6 mix) | ≤ 250 ms | 568 ms; 36 % errors | **miss** |
| Search service p95, 60 RPS direct (head queries, no gateway) | ≤ 250 ms | 144 ms; 2.5 % 503 | meets latency, not errors |
| LLM path p95 | ≤ 1200 ms | 967 ms at 60 RPS (replayed synthetic fixtures; not the live model) | indicative only |
| HTTP error rate | < 1 % | 36 % at 60 RPS, 99.9 % at 200 RPS | **miss** |
| Arrival rate achieved | < 1 % dropped at 200 RPS | 152 of 200 req/s (9,102 dropped) | **miss** |
| Index freshness p95 (outbox write → searchable) | ≤ 30 s | p50 6.8 s, p95 8.3 s (1k-row feed, workers agent run) | **meets** |
| Reranker top-30 p95, quiet CPU (`models/README.md`) | ≤ 120 ms | 105 ms (mMiniLM int8, 4 threads) | meets in isolation |

Stage budgets (ms, `config/budgets.yaml`; each is a `stage_latency` bucket edge so p95-vs-budget is exact):
normalise 5 · parse_rule 5 · semantic_cache 15 · parse_llm 800 · embed 50 · bm25 120 · knn 80 · fuse 5 ·
rerank 120 (re-split from 30/90/60 for embed/bm25/knn after the first runs; total still 250).

## How to measure

Prerequisites: core + obs + apps profiles up, migrations applied, catalogue imported and indexed
(see README quickstart).

```bash
docker compose -p souq -f infra/compose.yaml --profile obs --profile apps up -d --wait
curl -fsS localhost:4000/readyz                      # the k6 setup() aborts if this is not 200

# 200 RPS for 5 minutes (holds .locks/heavy-docker for the whole run):
loadtest/run.sh
# same, from inside the compose network (no host NAT hop), streaming k6 metrics into Prometheus:
K6_NETWORK=souq_default K6_PROMETHEUS_RW=1 loadtest/run.sh
# warm-cache variant (fixed page size → realistic gateway response-cache hits):
CACHE_MODE=warm loadtest/run.sh
```

`loadtest/results/summary.md` holds the table to paste into "Results"; `summary.json` the raw k6 data.
Exit code 99 means a threshold failed.

During the run, the per-stage breakdown comes from Prometheus (`http://localhost:9091`) or the
**Souq — Search SLO** dashboard (`http://localhost:3001/d/souq-search-slo`):

```promql
histogram_quantile(0.95, sum by (le, path)  (rate(search_latency_bucket[5m])))   # service-side p95 by path
histogram_quantile(0.95, sum by (le, stage) (rate(stage_latency_bucket[5m])))    # per-stage p95
sum by (stage) (rate(stage_budget_exceeded_total[5m]))                          # budget overruns
sum by (reason) (rate(degraded_total[5m]))                                      # degraded responses
```

## Load model (`loadtest/search.js`, `loadtest/queries.json`)

- k6 2.3.0 (`grafana/k6` image), `constant-arrival-rate` executors so a slow server cannot slow the offered
  load down (missed arrivals show up as `dropped_iterations`).
- 200 RPS total: **head** 150 req/s (EN 40 %, AR 30 %, Arabizi/mixed-script 15 %, explicit `filters[...]` 15 %,
  skewed towards popular queries), **long tail** 30 req/s (natural-language requests in EN/AR/Arabizi that the
  rule parser cannot resolve — LLM-path candidates), **deep pages** 5 iterations/s × 4 pages following
  `next_page_token` (20 req/s).
- Latency is attributed to the path the **server** reports (`parsed_query.parser`): `llm` → LLM-path trend,
  `rule`/`cache` → standard trend. A long-tail query the rule parser handles must still meet 250 ms.
- `CACHE_MODE=cold` (default) varies `limit` per request so most requests miss the gateway response cache and
  exercise the search pipeline; the measured gateway cache-hit ratio is reported in the summary.
- The gateway rate-limits 120 req/min per client IP; k6 spreads requests over 4 096 synthetic clients via
  `X-Forwarded-For` (the gateway runs with `trustProxy`), i.e. ~3 req/min per client.

## Results

**Run conditions.** Apple M-series laptop, 10 cores / 16 GB; Docker VM 10 vCPU / 7.7 GB shared by the whole
stack. Compose `apps` profile: `search` (2 uvicorn workers × 2 ORT threads, 4 GB limit) and `gateway`; core
data services in compose; k6 (`grafana/k6:2.3.0`) inside the compose network. Catalogue: `products_v2`,
51,001 docs, bge-m3 int8 embeddings. `SOUQ_EMBEDDER=bge-m3`, reranker mMiniLM-L12 int8,
`SOUQ_LLM_MODE=replay` (synthetic fixtures). Load model: 75 % head / 15 % long-tail / 10 % deep pages,
205-query pool, `CACHE_MODE=cold`. Uncommitted working tree (no git SHA yet).

### k6, 200 RPS × 5 min (final configuration)

| Latency (end-to-end at gateway) | p50 | p95 | p99 | max | n |
|---|---|---|---|---|---|
| standard path | 2,977 ms | 4,615 ms | 5,409 ms | 33,551 ms | 38,881 |
| LLM path | 2,963 ms | 4,590 ms | 5,371 ms | 33,508 ms | 7,520 |

Errors 99.94 % · achieved 152.4 req/s (9,102 dropped) · degraded 100 %. Both retrievers time out on
almost every request: offered load is ~3× capacity and nothing sheds it, so queues grow until every request
misses its deadline (congestion collapse). The service now **recovers on its own** within ~20 s of the load
stopping; before the connection-leak fix below it stayed at 100 % 503 until restarted.

### k6, 60 RPS × 2 min (run immediately after the 200 RPS run)

| Latency (end-to-end at gateway) | p50 | p95 | p99 | max | n |
|---|---|---|---|---|---|
| standard path | 163 ms | 568 ms | 1,034 ms | 1,488 ms | 6,567 |
| LLM path | 317 ms | 967 ms | 1,261 ms | 1,498 ms | 465 |

Errors 36.3 % · achieved 58.4 req/s · degraded 99.98 % (rerank shed by the breaker) · gateway cache hits 3.3 %.
Not isolated: whether the deep-page (PIT) mix, the gateway hop, or residue from the preceding overload run
accounts for the gap to the direct probe below.

### Direct probe of `services/search` (head queries only, no gateway, 20 s per step)

| Offered RPS | 200 OK | service p95 | kNN ran | reranked | search CPU |
|---|---|---|---|---|---|
| 30 | 96.8 % | 197 ms | 85 % | 6 % | 280 % |
| 60 | 97.5 % | 144 ms | 97 % | 3 % | 305 % |

Query embeddings are LRU-cached in-process and the probe's pool is small, so embed p50 is ~0.1 ms here;
real long-tail traffic embeds far more often (bge-m3 int8 ≈ 20 ms wall, ~40–60 ms CPU per query).

### Bottlenecks found while measuring (fixed in this pass)

1. **Cold start → 503.** A fresh search process blew the 60–90 ms retrieval budgets on its first requests.
   `souq_search.app.warm_up` now runs the embedder, reranker and OpenSearch client before `/readyz` passes.
2. **Workers OOM-killed.** ORT's CPU arena grew to the largest input shape seen and never shrank; 3 workers
   under 3.5–4 GB were killed repeatedly (114 restarts in 10 min). Arena and memory-pattern planning are off
   for both sessions; 2 workers × 2 threads runs with 0 restarts.
3. **Reranker burning CPU for nothing.** Timed-out inferences keep their slot until done, so under load the
   reranker used ~350 % of 520 % CPU at 30 RPS while nearly every result was discarded. A circuit breaker
   (`Reranker`, 20-attempt window, ≥ 50 % late → shed for 5 s) returns the fused order instead.
4. **Abandoned embeds piling up.** `EmbedService` shielded queued embeds, so timed-out requests still ran
   later; it now cancels work that has not started. ORT thread spinning is off (`SOUQ_ORT_SPIN=0`).
5. **Connection-pool leak wedged the service.** Cancelling httpx requests mid-flight leaked pooled connections;
   after a 5-minute overload all 200 were stuck and every call failed with `PoolTimeout` until restart.
   OpenSearch calls are now shielded from caller cancellation and end on their own budget-bounded timeout.

### What meeting 200 RPS at p95 ≤ 250 ms would take

- **Capacity:** at roughly 40–60 RPS per laptop-class search node, 200 RPS needs ~4 search replicas (or a GPU
  for bge-m3 and the cross-encoder), plus OpenSearch sized beyond a single 768 MB-heap node.
- **Admission control:** bound in-flight requests per worker and fail fast with 503 + `Retry-After` beyond
  capacity, so overload serves ~capacity successfully instead of collapsing to ~0.
- **Reranking:** at these rates on CPU it is almost entirely shed; the evals also show the mMiniLM reranker
  does not beat plain RRF (`evals/reports/final-offline.json`), so it costs latency budget for no quality.
- **Not yet investigated:** the deep-page / PIT path under load and the 60 RPS gap between k6 and the probe.

## Caveats that apply to any local measurement

- Everything (load generator, gateway, search, OpenSearch, Redis, the obs stack) shares one laptop and an 8 GB
  Docker VM that also runs an unrelated OpenTelemetry demo; OpenSearch is single-node with a 768 MB heap.
  Local numbers are a pessimistic lower bound on capacity, not a production SLO measurement.
- `SOUQ_LLM_MODE=replay` (no API key here) serves recorded LLM responses from `tests/fixtures/llm/`, so the
  LLM-path latency excludes real model latency, and long-tail queries without a fixture degrade to the rule
  parser (`degraded: ["llm_parse"]`). Validating the 1.2 s target needs a `live` run.
- `SOUQ_EMBEDDER=fake` skips bge-m3 inference; with the real embedder the `embed` stage (30 ms budget) is on the
  critical path of every kNN query.
- Collector tail sampling keeps all errors, all traces > 250 ms, every over-budget stage and every LLM-path
  trace, plus ~400 spans/s of the rest — so during a 200 RPS run most fast traces are intentionally dropped.

## Instrumentation verified (without the full stack)

Checked against the running obs profile (collector 0.162, Tempo 2.10, Prometheus 3.15, Grafana 13.2):

- **One trace per search across runtimes.** A Next 16 app using `apps/storefront/instrumentation.ts` →
  gateway process preloading `apps/gateway/dist/telemetry.js` → FastAPI service using
  `init_telemetry`/`instrument_fastapi` produced a single 22-span trace (Next render + fetch → HTTP/Fastify/
  GraphQL/ioredis/undici → `POST /search` → `stage.*`), with `souq.stage.budget_ms`/`souq.stage.exceeded` and
  `souq.parse.path` on the stage spans.
- **No query text in telemetry.** Raw OTLP payloads from the gateway, storefront and Python SDKs were decoded
  and searched for the query terms: none present (`url.query` → `REDACTED`, query strings stripped from
  `url.path`/`http.target`/`url.full`, GraphQL literals → `"*"`, redis FT.SEARCH docs dropped, only
  `souq.query.hash`). The collector applies the same rules again.
- **Metrics and exemplars.** All CLAUDE.md metric names arrive in Prometheus verbatim (`search_latency_bucket`,
  `consumer_lag`, …); histogram buckets carry `trace_id` exemplars that Grafana links to Tempo.
- **Rules.** `promtool test rules infra/prometheus/tests/souq-rules.test.yml` passes (burn-rate maths, ratio
  recording rules, freshness/DLQ/lag alerts).

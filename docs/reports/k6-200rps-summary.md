### k6 run 2026-10-14T08:10:38.479Z

Target `http://gateway:4000`, 200 req/s for 5m, cache mode `cold`, deep pages 4.
Achieved 55076 requests (182.5 req/s), dropped iterations 659.

| Latency (end-to-end at gateway) | p50 | p95 | p99 | max | n |
|---|---|---|---|---|---|
| standard path (rule/cache) | 215.6 ms | 1609.1 ms | 2602.6 ms | 14350.3 ms | 47434 |
| LLM path | 257.9 ms | 1775.2 ms | 2797.0 ms | 14045.2 ms | 7641 |
| all requests | 220.7 ms | 1624.6 ms | 2625.8 ms | 14350.3 ms | 55076 |

Errors 83.44% · checks 16.56% · LLM-path share 0.00% · degraded 99.93% · zero-result 6.48% · gateway cache hits 2.40%

| Metric | Threshold | Result |
|---|---|---|
| `checks` | `rate>0.99` | **FAIL** |
| `search_latency_llm` | `p(95)<1200` | **FAIL** |
| `search_latency_standard` | `p(95)<250` | **FAIL** |
| `http_req_failed` | `rate<0.01` | **FAIL** |
| `dropped_iterations` | `rate<2` | **FAIL** |

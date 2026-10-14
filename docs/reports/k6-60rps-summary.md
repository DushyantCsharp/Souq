### k6 run 2026-10-14T08:16:08.929Z

Target `http://gateway:4000`, 60 req/s for 2m, cache mode `cold`, deep pages 4.
Achieved 7331 requests (60.9 req/s), dropped iterations 6.

| Latency (end-to-end at gateway) | p50 | p95 | p99 | max | n |
|---|---|---|---|---|---|
| standard path (rule/cache) | 31.0 ms | 300.0 ms | 579.7 ms | 1022.5 ms | 7185 |
| LLM path | 270.1 ms | 704.9 ms | 845.7 ms | 914.8 ms | 145 |
| all requests | 31.5 ms | 321.5 ms | 615.6 ms | 1022.5 ms | 7331 |

Errors 11.29% · checks 88.70% · LLM-path share 0.00% · degraded 99.68% · zero-result 0.83% · gateway cache hits 4.24%

| Metric | Threshold | Result |
|---|---|---|
| `search_latency_standard` | `p(95)<250` | **FAIL** |
| `search_latency_llm` | `p(95)<1200` | PASS |
| `dropped_iterations` | `rate<1` | PASS |
| `http_req_failed` | `rate<0.01` | **FAIL** |
| `checks` | `rate>0.99` | **FAIL** |

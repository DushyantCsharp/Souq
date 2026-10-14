# observability — status (stopped on usage limit)

- DONE: souq_common/telemetry.py (all CLAUDE.md signatures + batch_span/continue_span/query_attributes/shutdown_telemetry, in-process PII redaction); apps/gateway/src/telemetry.ts; apps/storefront/instrumentation.ts (scaffold added src/ re-export + deps); infra/compose.obs.yaml + otel/tempo/prometheus/grafana configs; 2 dashboards; recording + alert rules; loadtest/{search.js,queries.json,run.sh}; docs/perf-report.md.
- VERIFIED: 28 telemetry unit tests + ruff + mypy pass; gateway typecheck/build/lint green; promtool check + `test rules` pass; collector config validates; obs stack up (`--profile obs`), synthetic + real-gateway spans/metrics/exemplars reach Tempo/Prometheus; every dashboard query returns data (gateway panels via real gateway run).
- VERIFIED end to end: one 22-span trace storefront(Next 16) → gateway → FastAPI search with stage budget/exceeded attrs; Kafka traceparent survives a real Redpanda broker (batch span links); decoded OTLP payloads contain no query text.
- Root-level pytest isolation issue (reported by scaffold): FIXED. Fixture forces OTEL_SDK_DISABLED=false and restores OTel globals on teardown; `uv run pytest -m "not integration and not live and not chaos"` → 603 passed, 1 skipped.
- NOT DONE: 200 RPS k6 measurement. The apps stack never became ready (gateway :4000 readyz down), so docs/perf-report.md says "not measured" everywhere. k6 script was smoke-tested only against a stub (results deleted).
- Resume:
  `docker compose -p souq -f infra/compose.yaml --profile obs --profile apps up -d --wait && curl -fsS localhost:4000/readyz`
  `loadtest/run.sh` (takes .locks/heavy-docker; 200 RPS × 5 min; results in loadtest/results/summary.md), then paste the summary + stage p95 PromQL from perf-report.md into its "Results" section.
- Handoffs filed: contracts (deps, dev --import, shutdownTelemetry, trustProxy note), scaffold (storefront deps/re-export/salt — done), workers (service names — done).

# Verification log — 2026-10-14

Results of the end-to-end checks run after the parallel build, on one laptop (Apple M-series, 10 cores,
16 GB; Docker VM 10 vCPU / 7.7 GB). Commands are from the repo root. Numbers are from those runs, not
estimates; load-test and eval figures live in their own reports (`docs/perf-report.md`, `docs/reports/`).

| Check | Command | Result |
|---|---|---|
| Full build, lint, typecheck, unit tests (TS + Python) | `pnpm turbo run build lint typecheck test` | 46/46 tasks green |
| Integration suite (testcontainers: Postgres, Redpanda, OpenSearch) | `SOUQ_IT_BUDGET_SCALE=10 uv run pytest tests/integration -m integration` | 81/81 passed (one test fixed: it probed kNN right after a realtime GET, before the index refreshed) |
| Chaos: OpenSearch killed mid-import, then restarted | `uv run pytest tests/chaos -m chaos` | passed — zero lost or duplicated SKUs, lag recovered (156 s run) |
| Reindex v1 → v2 with bge-m3 int8 | `SOUQ_EMBEDDER=bge-m3 SOUQ_ORT_THREADS=4 uv run --package souq-scripts python -I scripts/reindex.py --to v2` | 51,001 docs = Postgres count; 24 golden smoke queries + kNN hit; atomic read-alias swap |
| Rollback | `... reindex.py --rollback` | v1 rebuilt to 51,001 docs, verified, read alias swapped back |
| Forward again (incremental) | `... reindex.py --to v2` | caught up changed docs only; 19 s |
| Feed → outbox → relay → indexer → searchable | workers' shared-stack run | 1k-row feed: 990 ok / 10 rejected (= manifest); freshness p50 6.8 s, p95 8.3 s |
| 500k-row feed import | workers' scratch-DB run | 480 rows/s; 5,000 rejects = manifest row for row |
| Reranker latency, quiet CPU | `uv run --package souq-models python models/scripts/bench_rerankers.py --max-load 6` | mMiniLM-L12 int8 top-30 p50 72 ms / p95 105 ms; bge-reranker-v2-m3 int8 537 / 766 ms |
| Live LLM parse latency (Claude Haiku 5.5, 40 long-tail queries, after warm-up) | ad-hoc script, see perf report | p50 1,790 ms, p95 2,918 ms; 0 % within the 800 ms parse budget; 1,398 tokens/query mean |
| LLM-judge kappa gate (Haiku judge vs 50 Claude-graded reference pairs) | `SOUQ_LLM_MODE=record python -m souq_evals judge --slice ar --reference claude --sample 50` | kappa 0.599 < 0.6 → gate refuses; judge not used to label |

## Labels

- Arabic (2,078 pairs) and Arabizi/mixed (919 pairs) were graded by Claude at the maintainer's request and
  imported as `label_source: claude` (`evals/labelling/*_claude_graded.csv`); ESCI's human labels were kept
  on the misspelled-English queries' ESCI-judged pairs (212). They measure agreement with a model, not a
  human. Exact agreement with ESCI's grade for the English source query: 54–69 % per labelling batch.
- The kappa reference sample is `evals/labelling/kappa_reference_50.csv` (stratified 13/13/12/12 over 0–3).

## Translations

- First Claude Haiku 5.5 run (free-text output, adaptive thinking): reviewed sample of 100 was
  45 ok / 25 minor / 30 wrong; 25 of the 30 wrong were the model's own commentary leaking into the title.
  Kept for audit in `scripts/seed/translation_check_100.freetext-review.csv`; not indexed.
- Fix: strict JSON-schema output (`title_ar`), thinking off, validator rejecting non-Arabic/commentary output,
  title heads no longer cut on a dangling preposition. 12/12 previously-wrong titles came back clean in a
  live check before the full re-run.
- Second run (strict JSON, batch `msgbatch_019beVXX9dMXG1iY9kLeHjS9`): 9,565 translated, 176 rejected by the
  validator (left untranslated), 0 outputs with commentary. Reviewed sample of 100
  (`scripts/seed/translation_check_100.csv`, reviewed by Claude, not a human): **50 ok / 38 minor / 12 wrong**.
  Remaining wrong ones change meaning (a colour read as a brand, a size read as a model number, a product type
  mistranslated); the title head sometimes drops the product type. Next steps: translate the full title, try a
  stronger model, and keep a human spot check.
- 233 stale Arabic titles from earlier runs were deleted (products' versions bumped); 938 supplier-provided
  Arabic titles from the sample feed were kept.

## Reindex catch-up bug (found and fixed)

After loading the new titles straight into Postgres, a rollback + forward left `products_v2` with the old
titles: catch-up started from v2's detach time, after the bulk load. Fixed in `scripts/reindex.py` (catch-up
from the earlier of detach time and last verified sync; `--full` to force a rebuild), regression test added,
v2 rebuilt with `--to v2 --full`. Details in ADR 0002.

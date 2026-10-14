# data agent status (stopped on usage limit, 2026-10-13 23:20 SAST)
- DONE: `packages/db` migrations (6 + contract in `migrations-next/`), up/down/up verified; unit 7/7, integration 4/4, lint, typecheck. Applied on compose PG.
- DONE: `infra/opensearch/templates/products_v{1,2}.json`; `scripts/` (`souq-scripts`): reindex, backfill_size, templates, seeds; pytest 98 pass incl. char-filter parity on live OpenSearch; ruff + mypy strict clean.
- DONE: ESCI seed: 50,011 products / 2,558 queries / 51,713 judgments in `data/esci/*.parquet` and Postgres (`products` = 50,014 incl. 3 rows from other agents).
- DONE: 500k feed `data/feeds/supplier_500k.{csv,jsonl,manifest.json}` (5,000 bad rows) + 1k sample in `scripts/seed/samples/`.
- PARTIAL: Marian translation: 7,808 heads cached → 8,050/10,000 selected titles (8,977 products incl. shared heads) in Postgres `product_texts(ar)` + parquet; check sheet `scripts/seed/translation_check_100.csv`.
  Resume: `uv run --package souq-scripts --with 'transformers>=4.44,<5' --with 'torch>=2.4' --with sentencepiece --with sacremoses python -I scripts/seed/translate.py --provider marian --time-budget-s 600`
- PARTIAL: OpenSearch: `products_read`→v1 (3 docs, pre-seed init), `products_write`→v1+v2; v2 backfill checkpoint 16,000/50,014 (bge-m3; 20k vectors in `data/cache/embeddings-bge-m3.sqlite`).
  Resume (take `.locks/heavy-docker` first): `SOUQ_EMBEDDER=bge-m3 SOUQ_ORT_THREADS=8 uv run --package souq-scripts python -I scripts/reindex.py --to v2` (resumes after the checkpoint, verifies, smoke-tests, swaps).
  Then the rollback cycle: `... reindex.py --rollback` and `... reindex.py --to v2` (same env). Use `--dry-run` to preview any step.
- NOT RUN on compose: `backfill_size.py` had 0 pending rows (the seed ran after the expand migration); a real pre-expand backfill is covered by `scripts/tests/test_backfill_size_pg.py`.
- NOTE: `.env` (created by another agent) contains an `sk-ant-` key. I did not use it; `--provider auto` only honours an exported `ANTHROPIC_API_KEY`.
- Handoffs answered: workers, search, cicd, tests, scaffold (see `docs/handoffs/*.md`).

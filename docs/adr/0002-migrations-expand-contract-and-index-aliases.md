# ADR 0002: Expand/contract schema migrations and versioned OpenSearch indices behind aliases

- Status: Accepted
- Date: 2026-10-14
- Scope: `packages/db` (node-pg-migrate), `scripts/backfill_size.py`, `scripts/reindex.py`,
  `infra/opensearch/templates/products_v{1,2}.json`, the indexer's alias resolver, CI (`ci.yml`).

## Context

Two stores change shape over time, and neither change may take search or feed imports offline:

- **Postgres** is written by the feeds importer and read by `product_document()`, the relay and
  the search service's query log. During a rolling deploy, old and new code run side by side against one
  schema. The first real case is moving `size` out of the `attributes` jsonb into a typed `products.size`
  column.
- **OpenSearch** mappings and analyzers cannot change in place for most changes, such as a new analyzer
  or the `title_*.prefix` edge-ngram subfields that v2 adds. A different embedder also means every
  document vector has to be recomputed. Search must keep serving the old index until the new one is
  complete and checked, and a bad new index must be reversible.

## Decision

### Postgres: node-pg-migrate, expand/contract

Migrations are plain SQL files with `-- Up Migration` / `-- Down Migration` sections, run by
node-pg-migrate (`pnpm --filter @souq/db migrate up`). A change never drops or renames something in the
release that adds its replacement (CLAUDE.md, "Conventions"). For `attributes.size` → `products.size`:

1. **Expand (release N)**, `packages/db/migrations/1791849600500_expand_product_size.sql`: add a nullable
   `size` column. A `BEFORE INSERT OR UPDATE` trigger (`products_sync_size`) keeps the column and
   `attributes.size` in sync in both directions, so old writers (jsonb only) and new writers (column) both
   work. `product_document()` reads `coalesce(p.size, p.attributes ->> 'size')`.
2. **Backfill**, `scripts/backfill_size.py`: copies `attributes.size` into the column for rows written
   before the expand migration. It works in keyset batches (`ORDER BY sku LIMIT n FOR UPDATE`, commit per
   batch) with `lock_timeout` and a pause between batches. That way no writer waits behind one long
   statement, and the work avoids one large WAL burst and an all-or-nothing rollback. Re-running resumes,
   because already-copied rows no longer match the predicate. `souq.skip_version_bump` is set per batch,
   because copying a value into a new column does not change `product_document()` and must not emit
   outbox events.
3. **Contract (release N+1)**, `packages/db/migrations-next/1791936000000_contract_product_size.sql`:
   catches up any row the backfill missed, drops the trigger, strips `size` from `attributes`, and makes
   `product_document()` read the column only. It lives in `migrations-next/`, so `migrate up` cannot
   apply it early; `migrate:next` applies it once every reader and writer uses the column. Its down
   section restores the trigger and the jsonb key, so the contract step can be rolled back on its own.

### OpenSearch: versioned indices behind two aliases

Indices are `products_v1`, `products_v2`, …. Search reads `products_read`, and the indexer writes to every
concrete index behind `products_write`, re-resolving the alias every 10 s. `scripts/reindex.py --to vN`
runs these steps, each idempotent so a crashed run resumes when re-run:

1. Validate and install the template, then create the index with refresh off.
2. Add the new index to `products_write` and wait longer than the indexer's alias refresh. From then on
   every live change is **dual-written** to the old and the new index.
3. Backfill from Postgres `product_document()` plus embeddings, with `version_type=external`, so a newer
   live write always beats an older backfill copy (409 = fine). Progress is checkpointed in the index
   mapping's `_meta`.
4. Verify: doc count equals the Postgres row count, and 24 golden queries sampled from
   `evals/golden/*.jsonl` (lexical and kNN) each return a hit.
5. Swap `products_read` in **one** `_aliases` call (atomic), then detach the old index from
   `products_write` and record when it stopped receiving writes.

`reindex.py --rollback` re-attaches the previous index to `products_write`, catches it up with rows changed
since it was detached (and prunes deleted SKUs), verifies it, and swaps `products_read` back in one call.
The newer index is marked stale, so a later `--to` only needs an incremental catch-up. `reindex.py` refuses
to mix embedders in one index (`_meta.souq.backfill.embedder`).

## Alternatives considered

| Option | Why not |
|---|---|
| One migration that adds the column, runs a single `UPDATE products SET size = …`, and drops the jsonb key | Row-locks every product until commit, so feed upserts queue behind it. One giant WAL burst. A failure near the end rolls back everything. Old code still running during the deploy loses the key it reads. |
| Rename or retype in place (`ALTER COLUMN … TYPE`, `RENAME`) | Breaks whichever code version is not yet deployed during a rolling deploy, and some type changes rewrite the table under an exclusive lock. |
| Python migration tool (Alembic) or an ORM's migrations | Possible, but migrations are raw SQL either way. node-pg-migrate keeps up and down in one file, and the TypeScript workspace already owns the schema package. Nothing ranking-related depends on the choice. |
| Change mappings on the live index | Only additive field changes work in place. Analyzer changes, new subfields on existing text fields and a new embedder all need a new index. |
| OpenSearch `_reindex` from v1 to v2 | Copies the old index's documents instead of rebuilding from Postgres, the source of truth, so earlier drift would be carried over. It cannot recompute embeddings when the embedder changes. |
| Write only to the new index during the backfill | Loses rollback: once reads move, the old index stops being current. |
| A second cluster (blue/green) | Doubles the infrastructure for a problem that two indices behind aliases solve on one cluster. |

## Consequences

- **Every schema move takes two releases plus a backfill.** The contract file has to wait in
  `migrations-next/`, and someone has to apply it. Until then the sync trigger fires on every product
  write. It is one of the per-row triggers in the import cost described in ADR 0001.
- **Down migrations are real code and are tested.** The CI round-trip catches a down section that forgets
  an object or recreates it differently.
- **Reindexing costs indexing capacity and disk.** During dual-write every change is indexed twice, and two
  full indices (with 1024-d vectors) exist at once. Reindex time is bounded by document-embedding
  throughput. See [docs/scaling.md](../scaling.md) for what that means at 5M SKUs.
- **Rollback is cheap only while the old index is close to current.** In steady state it is an incremental
  catch-up. If the old index was detached long ago, or was never filled, rollback rebuilds it, as in the
  measured run below.
- **The smoke test is catalog-specific.** The 24 golden queries assume the ESCI seed catalog. Any other
  catalog needs `--golden-file` (see `docs/handoffs/data.md`).

## Evidence

| Claim | Measurement | Source |
|---|---|---|
| Backfill of a new index | `products_v2`: 51,001 docs with bge-m3 int8 embeddings, ~17 min, at ~30–44 docs/s on 4 ORT threads. With 8 ORT threads on an oversubscribed host it stalled to ~2 docs/s, so `SOUQ_ORT_THREADS=4`. The run resumed from a checkpoint with cached vectors (`docs/handoffs/data-STATUS.md`), so 51,001 ÷ 17 min is not a throughput figure | `models/README.md` |
| Checks before the swap | Doc count matched Postgres and all 24 golden smoke queries hit before `products_read` moved to v2 | `scripts/reindex.py` steps 4–5 |
| Rollback works | `--rollback` rebuilt `products_v1` to 51,001 docs (it held only the pre-seed init docs) and swapped `products_read` back | reindex run, 2026-10-14 |
| Forward again is incremental | `--to v2` after the rollback caught up incrementally in 19 s and swapped | reindex run, 2026-10-14 |
| No errors during a swap under traffic | `test_alias_swap_mid_traffic_has_no_errors` passes (search loop and writer loop running while the alias moves) | `tests/integration/test_alias_swap.py` |
| Migrations are reversible | CI runs up → down all → up with a schema diff at each step, then applies and rolls back the staged contract migration on its own, then a full round-trip including `migrations-next/` | `ci/migrations_roundtrip.sh`, `.github/workflows/ci.yml` |
| Backfill under concurrent writers on a production-size table, dual-write indexing overhead, reindex at 5M SKUs | not measured | — |

## Found in use: catch-up missed writes that bypass the outbox (fixed)

Incremental catch-up assumed that an index inside `products_write` receives every change through the indexer,
so it caught up from the moment the index left the alias (`detached_at` − 5 min). Bulk loads that write
Postgres directly — seeds, the Arabic translation load — never pass through the outbox or the indexer. On
2026-10-14, 10.6k re-translated titles were loaded while `products_v2` was live; after a rollback and a
forward migration, v2 was "caught up" from its detach time and kept the old titles (the doc-count check could
not notice: they were updates, not inserts).

Fix: catch-up starts from the earlier of `detached_at` and the last time the index was verified complete
(`backfill.finished_at`), and `reindex.py --full` forces a rebuild for repair.
`test_forward_again_catches_up_rows_written_straight_to_postgres` reproduces the timeline and fails on the old
logic. The rule this encodes: an index is only as current as its last verified sync, whatever alias it sits in.

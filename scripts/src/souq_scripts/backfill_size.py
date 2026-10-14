"""Backfill ``products.size`` from ``attributes->>'size'`` (expand phase of the attributes.size move).

Run after packages/db/migrations/*_expand_product_size.sql and before the contract migration in
packages/db/migrations-next/. Rows written after the expand migration are already in sync (trigger);
this only handles rows written before it.

Why batches instead of one ``UPDATE products SET size = ...``:
  * a single statement row-locks every product until it commits, so the feeds importer's upserts queue
    behind it for the whole run; keyset batches of ``--batch-size`` rows hold each lock for milliseconds;
  * one giant transaction is one giant WAL burst (replica lag) and, if anything fails at row 900k, all of
    it rolls back; per-batch commits keep progress, and re-running resumes for free because the
    predicate (``size IS NULL AND attributes ? 'size'``) skips rows already done;
  * ``lock_timeout`` makes a batch give up instead of waiting behind a long writer; it is retried after a
    pause, and ``--sleep-ms`` between batches leaves headroom for live traffic.
Rows locked by a writer are waited for (no SKIP LOCKED): skipping them would leave them behind, since a
writer that does not touch attributes.size does not fill the new column.

``souq.skip_version_bump`` is set per batch: copying a value into a new column does not change
product_document(), so it must not bump versions or look like a product change downstream.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

import psycopg

from souq_scripts.db import connect

BATCH_SQL = """
WITH batch AS (
  SELECT sku FROM products
   WHERE sku > %(after)s AND size IS NULL AND attributes ? 'size'
   ORDER BY sku
   LIMIT %(limit)s
   FOR UPDATE
)
UPDATE products p
   SET size = p.attributes ->> 'size'
  FROM batch
 WHERE p.sku = batch.sku
RETURNING p.sku
"""
PENDING_SQL = "SELECT count(*) FROM products WHERE size IS NULL AND attributes ? 'size'"


def log(msg: str) -> None:
    print(f"[backfill_size] {msg}", file=sys.stderr, flush=True)


def has_size_column(conn: psycopg.Connection[Any]) -> bool:
    row = conn.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name = 'products' AND column_name = 'size'"
    ).fetchone()
    return row is not None


def pending(conn: psycopg.Connection[Any]) -> int:
    row = conn.execute(PENDING_SQL).fetchone()
    return int(row[0]) if row else 0


def run_batch(conn: psycopg.Connection[Any], after: str, limit: int, lock_timeout_ms: int) -> list[str]:
    with conn.transaction():
        conn.execute(f"SET LOCAL lock_timeout = '{int(lock_timeout_ms)}ms'")
        conn.execute("SET LOCAL souq.skip_version_bump = 'on'")
        rows = conn.execute(BATCH_SQL, {"after": after, "limit": limit}).fetchall()
    return sorted(str(r[0]) for r in rows)


def backfill(
    conn: psycopg.Connection[Any],
    *,
    batch_size: int = 2000,
    sleep_ms: int = 50,
    lock_timeout_ms: int = 2000,
    max_lock_retries: int = 10,
) -> int:
    after, total, retries = "", 0, 0
    while True:
        try:
            skus = run_batch(conn, after, batch_size, lock_timeout_ms)
        except psycopg.errors.LockNotAvailable:
            retries += 1
            if retries > max_lock_retries:
                raise
            log(f"batch after {after!r} hit lock_timeout; retry {retries}/{max_lock_retries}")
            time.sleep(min(5.0, 0.2 * 2**retries))
            continue
        retries = 0
        if not skus:
            return total
        total += len(skus)
        after = skus[-1]
        if total % (batch_size * 10) < batch_size:
            log(f"{total} rows backfilled (last sku {after})")
        if sleep_ms:
            time.sleep(sleep_ms / 1000)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database-url", default=None)
    ap.add_argument("--batch-size", type=int, default=2000)
    ap.add_argument("--sleep-ms", type=int, default=50)
    ap.add_argument("--lock-timeout-ms", type=int, default=2000)
    ap.add_argument("--dry-run", action="store_true", help="only report how many rows need backfilling")
    args = ap.parse_args(argv)
    with connect(args.database_url, autocommit=True) as conn:
        if not has_size_column(conn):
            log(
                "products.size does not exist: apply the expand migration first "
                "(pnpm --filter @souq/db migrate up)"
            )
            sys.exit(1)
        before = pending(conn)
        log(f"{before} rows have attributes.size but no size column value")
        if args.dry_run or before == 0:
            return
        started = time.monotonic()
        n = backfill(
            conn, batch_size=args.batch_size, sleep_ms=args.sleep_ms, lock_timeout_ms=args.lock_timeout_ms
        )
        left = pending(conn)
        log(f"backfilled {n} rows in {time.monotonic() - started:.1f}s; {left} still pending")
        if left:
            # Only possible if an old-style writer inserted rows behind the keyset cursor without the
            # trigger (i.e. before the expand migration finished deploying): run again.
            sys.exit(2)


if __name__ == "__main__":
    main()

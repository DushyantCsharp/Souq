"""Streaming feed import: parse -> validate -> 1k-row transactions (products + product_texts + outbox).

Per batch, in ONE transaction:

1. COPY the validated rows into a temp staging table.
2. Re-check category paths with the SQL ``category_ltree()`` (the database is the authority).
3. Read the pre-image versions, upsert ``products`` and ``product_texts`` (no-op rows are skipped by
   ``WHERE ... IS DISTINCT FROM``), read the post-image versions. ``products.version`` is bumped by the
   packages/db triggers on any effective change (including title/description changes).
4. For SKUs whose version changed (or that are new) insert ``outbox`` rows with
   ``payload = product_document(sku)`` and the batch span's W3C ``trace_context``.
5. COPY the batch's row errors into ``feed_row_errors`` and update the feed's progress counters.

So an unchanged re-import writes nothing and emits no events, and a crash never leaves products
without their outbox events. Row problems never abort a feed; only structural problems do
(``StructuralError``: unreadable encoding, missing header columns, unsupported format).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from opentelemetry import trace
from psycopg import sql
from psycopg.rows import dict_row

from souq_common.telemetry import context_from_carrier, inject_carrier
from souq_feeds.parsing import ParsedRow, StructuralError, iter_rows
from souq_feeds.validation import FeedRow, RowRejection, validate_row

log = logging.getLogger(__name__)
_tracer = trace.get_tracer("souq.feeds")

MAX_MESSAGE_CHARS = 1000

STAGE_COLUMNS = (
    "sku",
    "brand",
    "category_path",
    "price",
    "currency",
    "colour",
    "size",
    "attributes",
    "title_en",
    "title_ar",
    "description_en",
    "description_ar",
)

STAGE_DDL = """
CREATE TEMP TABLE IF NOT EXISTS feed_stage (
  sku text PRIMARY KEY,
  brand text,
  category_path text NOT NULL,
  price numeric(12, 2) NOT NULL,
  currency text NOT NULL,
  colour text,
  size text,
  attributes jsonb NOT NULL,
  title_en text,
  title_ar text,
  description_en text,
  description_ar text
) ON COMMIT DELETE ROWS
"""

BAD_CATEGORIES = "SELECT sku, category_path FROM feed_stage WHERE category_ltree(category_path) IS NULL"
DROP_STAGED = "DELETE FROM feed_stage WHERE sku = ANY(%(skus)s)"
VERSIONS = "SELECT p.sku, p.version FROM products p JOIN feed_stage s ON s.sku = p.sku"

# ORDER BY sku: concurrent imports touching the same SKUs lock rows in the same order (no deadlocks).
UPSERT_PRODUCTS = """
INSERT INTO products AS p (sku, brand, category_path, price, currency, colour, size, attributes)
SELECT sku, brand, category_ltree(category_path), price, currency, colour, size, attributes
FROM feed_stage
ORDER BY sku
ON CONFLICT (sku) DO UPDATE SET
  brand = EXCLUDED.brand,
  category_path = EXCLUDED.category_path,
  price = EXCLUDED.price,
  currency = EXCLUDED.currency,
  colour = EXCLUDED.colour,
  size = EXCLUDED.size,
  attributes = EXCLUDED.attributes
WHERE (p.brand, p.category_path, p.price, p.currency, p.colour, p.size, p.attributes)
      IS DISTINCT FROM
      (EXCLUDED.brand, EXCLUDED.category_path, EXCLUDED.price, EXCLUDED.currency, EXCLUDED.colour,
       EXCLUDED.size, EXCLUDED.attributes)
"""

# A feed row is the full state of the languages it carries; a language it omits is left untouched
# (e.g. Arabic titles produced by the translation seed survive an English-only supplier feed).
UPSERT_TEXTS = """
INSERT INTO product_texts AS t (sku, lang, title, description)
SELECT sku, lang, title, description FROM (
  SELECT sku, 'en' AS lang, title_en AS title, description_en AS description
  FROM feed_stage WHERE title_en IS NOT NULL
  UNION ALL
  SELECT sku, 'ar', title_ar, description_ar FROM feed_stage WHERE title_ar IS NOT NULL
) rows
ORDER BY sku, lang
ON CONFLICT (sku, lang) DO UPDATE SET title = EXCLUDED.title, description = EXCLUDED.description
WHERE (t.title, t.description) IS DISTINCT FROM (EXCLUDED.title, EXCLUDED.description)
"""

INSERT_OUTBOX = """
INSERT INTO outbox (aggregate_id, event_type, payload, trace_context)
SELECT s, 'product.upserted', product_document(s), %(trace_context)s::jsonb
FROM unnest(%(skus)s::text[]) WITH ORDINALITY AS u(s, ord)
ORDER BY ord
"""

UPDATE_PROGRESS = """
UPDATE feed_imports
SET rows_total = %(total)s, rows_ok = %(ok)s, rows_rejected = %(rejected)s, error_counts = %(counts)s::jsonb
WHERE id = %(id)s
"""


@dataclass(frozen=True, slots=True)
class RowError:
    row_number: int
    code: str
    field: str | None
    message: str
    raw: str


@dataclass(slots=True)
class ImportCounters:
    total: int = 0
    ok: int = 0
    rejected: int = 0
    errors: Counter[str] = field(default_factory=Counter)
    changed: int = 0
    batches: int = 0

    def as_params(self, feed_id: UUID) -> dict[str, Any]:
        return {
            "id": feed_id,
            "total": self.total,
            "ok": self.ok,
            "rejected": self.rejected,
            "counts": json.dumps(dict(sorted(self.errors.items()))),
        }


@dataclass(frozen=True, slots=True)
class ImportOutcome:
    status: str
    counters: ImportCounters
    failure_reason: str | None
    seconds: float


def _stage_tuple(row: FeedRow) -> tuple[Any, ...]:
    return (
        row.sku,
        row.brand,
        row.category_path,
        row.price,
        row.currency,
        row.colour,
        row.size,
        json.dumps(row.attributes, ensure_ascii=False),
        row.title_en,
        row.title_ar,
        row.description_en,
        row.description_ar,
    )


class BatchWriter:
    """Writes one batch per transaction on a dedicated connection (autocommit mode)."""

    def __init__(self, conn: psycopg.Connection[Any], feed_id: UUID) -> None:
        self._conn = conn
        self._feed_id = feed_id
        self._staged = False

    def write(
        self,
        rows: list[tuple[int, FeedRow, str]],
        errors: list[RowError],
        counters: ImportCounters,
    ) -> None:
        """Write rows + errors atomically; counters are updated only if the transaction commits."""
        late: list[RowError] = []
        changed: list[str] = []
        with _tracer.start_as_current_span("feeds.batch") as span:
            span.set_attribute("souq.feeds.rows", len(rows))
            # Captured inside the batch span so relay/indexer spans hang off this batch.
            carrier = json.dumps(inject_carrier())
            with self._conn.transaction(), self._conn.cursor(row_factory=dict_row) as cur:
                if not self._staged:
                    cur.execute(STAGE_DDL)
                if rows:
                    by_sku = {r.sku: (n, raw) for n, r, raw in rows}
                    cols = sql.SQL(", ").join(sql.Identifier(c) for c in STAGE_COLUMNS)
                    with cur.copy(sql.SQL("COPY feed_stage ({}) FROM STDIN").format(cols)) as copy:
                        for _, row, _ in rows:
                            copy.write_row(_stage_tuple(row))
                    bad = cur.execute(BAD_CATEGORIES).fetchall()
                    if bad:
                        skus = [b["sku"] for b in bad]
                        cur.execute(DROP_STAGED, {"skus": skus})
                        for b in bad:
                            n, raw = by_sku[b["sku"]]
                            late.append(
                                RowError(
                                    n,
                                    "invalid_category",
                                    "category_path",
                                    "category_path is not a valid category",
                                    raw,
                                )
                            )
                    pre = {r["sku"]: r["version"] for r in cur.execute(VERSIONS).fetchall()}
                    t0 = time.perf_counter()
                    cur.execute(UPSERT_PRODUCTS)
                    t1 = time.perf_counter()
                    cur.execute(UPSERT_TEXTS)
                    t2 = time.perf_counter()
                    # Where a slow batch spends its time (the texts upsert fires the version triggers).
                    span.set_attribute("souq.feeds.upsert_products_ms", round((t1 - t0) * 1000, 1))
                    span.set_attribute("souq.feeds.upsert_texts_ms", round((t2 - t1) * 1000, 1))
                    post = {r["sku"]: r["version"] for r in cur.execute(VERSIONS).fetchall()}
                    changed = [s for s in sorted(post) if pre.get(s) != post[s]]
                    if changed:
                        cur.execute(INSERT_OUTBOX, {"skus": changed, "trace_context": carrier})
                all_errors = errors + late
                if all_errors:
                    with cur.copy(
                        "COPY feed_row_errors (feed_id, row_number, code, field, message, raw) FROM STDIN"
                    ) as copy:
                        for e in sorted(all_errors, key=lambda e: e.row_number):
                            copy.write_row(
                                (
                                    self._feed_id,
                                    e.row_number,
                                    e.code,
                                    e.field,
                                    e.message[:MAX_MESSAGE_CHARS],
                                    e.raw,
                                )
                            )
                staged_ok = len(rows) - len(late)
                pending = ImportCounters(
                    total=counters.total,
                    ok=counters.ok + staged_ok,
                    rejected=counters.rejected + len(all_errors),
                    errors=counters.errors + Counter(e.code for e in all_errors),
                )
                cur.execute(UPDATE_PROGRESS, pending.as_params(self._feed_id))
            span.set_attribute("souq.feeds.changed", len(changed))
        self._staged = True  # only once the CREATE TEMP TABLE has committed
        counters.ok = pending.ok
        counters.rejected = pending.rejected
        counters.errors = pending.errors
        counters.changed += len(changed)
        counters.batches += 1


class RowSorter:
    """Parsed row -> validated row or reject. Duplicate detection keeps the first *valid* occurrence, so a
    broken first copy does not cost the supplier the product. Holds only the set of SKUs seen."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def accept(self, parsed: ParsedRow) -> tuple[FeedRow | None, RowError | None]:
        if parsed.error is not None or parsed.data is None:
            err = parsed.error or RowRejection("malformed_row", None, "unparseable row")
            return None, RowError(parsed.row_number, err.code, err.field, err.message, parsed.raw)
        try:
            row = validate_row(parsed.data)
        except RowRejection as rej:
            return None, RowError(parsed.row_number, rej.code, rej.field, rej.message, parsed.raw)
        if row.sku in self._seen:
            message = f"sku {row.sku} already appeared earlier in this feed"
            return None, RowError(parsed.row_number, "duplicate_sku_in_feed", "sku", message, parsed.raw)
        self._seen.add(row.sku)
        return row, None


class ImportInterruptedError(Exception):
    """Shutdown requested; the feed goes back to 'queued' and is re-run (idempotently) on restart."""


class FeedImporter:
    def __init__(
        self,
        conn: psycopg.Connection[Any],
        feed_id: UUID,
        batch_size: int = 1000,
        stop_event: threading.Event | None = None,
    ) -> None:
        self._conn = conn
        self._feed_id = feed_id
        self._batch_size = batch_size
        self._stop = stop_event
        self._writer = BatchWriter(conn, feed_id)
        self.counters = ImportCounters()

    def _flush(self, rows: list[tuple[int, FeedRow, str]], errors: list[RowError]) -> None:
        try:
            self._writer.write(rows, errors, self.counters)
        except (psycopg.errors.DataError, psycopg.errors.IntegrityError) as exc:
            # A row the validator accepted but the database refused: isolate it by bisection so the rest
            # of the batch still lands, and reject just that row.
            if len(rows) <= 1:
                detail = exc.diag.message_primary or str(exc)
                rejected = [
                    RowError(n, "malformed_row", None, f"rejected by database: {detail}", raw)
                    for n, _, raw in rows
                ]
                self._writer.write([], errors + rejected, self.counters)
                return
            mid = len(rows) // 2
            self._flush(rows[:mid], errors)
            self._flush(rows[mid:], [])

    def run(self, path: Path, fmt: str) -> None:
        """Import ``path``. Raises ``StructuralError`` (after committing what was valid so far)."""
        sorter = RowSorter()
        rows: list[tuple[int, FeedRow, str]] = []
        errors: list[RowError] = []
        try:
            for parsed in iter_rows(path, fmt):
                self.counters.total += 1
                ok, error = sorter.accept(parsed)
                if ok is not None:
                    rows.append((parsed.row_number, ok, parsed.raw))
                elif error is not None:
                    errors.append(error)
                if len(rows) >= self._batch_size or len(errors) >= self._batch_size:
                    self._flush(rows, errors)
                    rows, errors = [], []
                    if self._stop is not None and self._stop.is_set():
                        raise ImportInterruptedError
        except StructuralError:
            # Keep what was valid before the structural fault (earlier batches are committed anyway).
            if rows or errors:
                self._flush(rows, errors)
            raise
        if rows or errors or self.counters.batches == 0:
            self._flush(rows, errors)


# --------------------------------------------------------------------------- feed lifecycle

CLAIM = """
UPDATE feed_imports
SET status = 'running', started_at = now(), finished_at = NULL, failure_reason = NULL,
    rows_total = 0, rows_ok = 0, rows_rejected = 0, error_counts = '{}'
WHERE id = %(id)s AND status IN ('queued', 'running')
RETURNING id, format, file_path, trace_context
"""

FINISH = """
UPDATE feed_imports
SET status = %(status)s, finished_at = now(), failure_reason = %(reason)s,
    rows_total = %(total)s, rows_ok = %(ok)s, rows_rejected = %(rejected)s, error_counts = %(counts)s::jsonb
WHERE id = %(id)s
"""


def _lock_key(feed_id: UUID) -> int:
    # Signed 64-bit advisory-lock key derived from the UUID: one importer per feed across replicas.
    return int.from_bytes(feed_id.bytes[:8], "big", signed=True)


REQUEUE = "UPDATE feed_imports SET status = 'queued', started_at = NULL WHERE id = %(id)s"


def run_import(
    conn: psycopg.Connection[Any],
    feed_id: UUID,
    batch_size: int = 1000,
    stop_event: threading.Event | None = None,
) -> ImportOutcome | None:
    """Claim and import one feed on ``conn`` (autocommit). Returns None if someone else holds it."""
    started = time.perf_counter()
    key = _lock_key(feed_id)
    got = conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (key,)).fetchone()
    if not got or not got[0]:
        return None
    try:
        with conn.transaction():
            claimed = conn.execute(CLAIM, {"id": feed_id}).fetchone()
            if claimed is None:
                return None
            # A re-run (service restart) starts over; the import is idempotent, its error list is not.
            conn.execute("DELETE FROM feed_row_errors WHERE feed_id = %s", (feed_id,))
        _, fmt, file_path, trace_context = claimed
        parent = context_from_carrier({str(k): str(v) for k, v in (trace_context or {}).items()})
        importer = FeedImporter(conn, feed_id, batch_size, stop_event)
        status, reason = "completed", None
        with _tracer.start_as_current_span("feeds.import", context=parent) as span:
            span.set_attribute("souq.feed.id", str(feed_id))
            span.set_attribute("souq.feed.format", fmt)
            try:
                if not file_path or not Path(file_path).is_file():
                    raise StructuralError("uploaded file is no longer available")
                importer.run(Path(file_path), fmt)
            except ImportInterruptedError:
                with conn.transaction():
                    conn.execute(REQUEUE, {"id": feed_id})
                log.info("feed %s interrupted after %d rows; re-queued", feed_id, importer.counters.total)
                return None
            except StructuralError as exc:
                status, reason = "failed", str(exc)
            except Exception as exc:  # an infrastructure fault, not a row problem
                log.exception("feed %s import crashed", feed_id)
                status, reason = "failed", f"internal error: {exc.__class__.__name__}: {exc}"[:1000]
            c = importer.counters
            span.set_attribute("souq.feeds.rows_total", c.total)
            span.set_attribute("souq.feeds.rows_rejected", c.rejected)
        params = c.as_params(feed_id) | {"status": status, "reason": reason}
        with conn.transaction():
            conn.execute(FINISH, params)
        elapsed = time.perf_counter() - started
        log.info(
            "feed %s %s: %d rows (%d ok, %d rejected, %d changed) in %.1fs (%.0f rows/s)%s",
            feed_id,
            status,
            c.total,
            c.ok,
            c.rejected,
            c.changed,
            elapsed,
            c.total / elapsed if elapsed else 0.0,
            f": {reason}" if reason else "",
        )
        return ImportOutcome(status, c, reason, elapsed)
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (key,))

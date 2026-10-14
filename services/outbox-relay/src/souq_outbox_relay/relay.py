"""Outbox relay loop.

Each iteration, in ONE Postgres transaction:

1. ``SELECT ... FROM outbox WHERE published_at IS NULL ORDER BY id LIMIT 500 FOR UPDATE SKIP LOCKED``
2. produce every row to ``catalog.changes`` (key = sku) with the idempotent producer
3. wait until Kafka acknowledged every message (acks=all)
4. ``UPDATE outbox SET published_at = now()`` for those ids and COMMIT

A crash or delivery failure before the commit rolls back, the rows stay unpublished and are produced
again: delivery is at-least-once, and the indexer is idempotent (external versions; 409 = success).

Two replicas are safe: SKIP LOCKED gives each one a disjoint set of rows, so nothing is published twice
by concurrent relays. **Ordering caveat:** replica A may lock ids 1-500 while replica B locks 501-1000;
if the same SKU has v3 in A's batch and v4 in B's, B can publish v4 first, so Kafka (and the indexer)
may see v4 before v3. That is safe because every event carries the full product document plus
``products.version``, and the indexer writes with ``version_type=external``: the late v3 gets a 409 and
is dropped, so the index converges to the newest version regardless of arrival order. (Deletes are
covered the same way for as long as OpenSearch retains the tombstone — ``index.gc_deletes``.)
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import psycopg
from opentelemetry import trace
from opentelemetry.trace import SpanKind
from psycopg.rows import dict_row

from souq_common.telemetry import context_from_carrier, inject_kafka_headers
from souq_outbox_relay.kafka import OutgoingMessage, produce_and_wait

try:  # transitively installed with the psycopg auto-instrumentation (souq_common.telemetry)
    from opentelemetry.instrumentation.utils import suppress_instrumentation
except ImportError:  # pragma: no cover - instrumentation not installed: nothing to suppress
    suppress_instrumentation = contextlib.nullcontext  # type: ignore[assignment]

log = logging.getLogger(__name__)
_tracer = trace.get_tracer("souq.outbox_relay")

SELECT_BATCH = """
SELECT id, aggregate_id, event_type, payload, trace_context, created_at
FROM outbox
WHERE published_at IS NULL
ORDER BY id
LIMIT %(limit)s
FOR UPDATE SKIP LOCKED
"""

MARK_PUBLISHED = "UPDATE outbox SET published_at = now() WHERE id = ANY(%(ids)s)"

BACKLOG = "SELECT count(*) AS n, min(created_at) AS oldest FROM outbox WHERE published_at IS NULL"

# Chunked so pruning never holds long locks or bloats WAL in one go.
PRUNE = """
DELETE FROM outbox WHERE id IN (
  SELECT id FROM outbox
  WHERE published_at IS NOT NULL AND published_at < now() - make_interval(secs => %(secs)s)
  LIMIT 5000
)
"""


def iso_ms(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _as_dict(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str | bytes):
        decoded = json.loads(value)
        return decoded if isinstance(decoded, dict) else None
    return None


def build_message(row: dict[str, Any], topic: str) -> OutgoingMessage:
    """Outbox row -> catalog.changes message (value + headers per the CLAUDE.md Kafka contract)."""
    payload = _as_dict(row["payload"])
    sku = str(row["aggregate_id"])
    version = payload.get("version") if payload else None
    # A payload without a version cannot be indexed safely; publish it anyway (never wedge the outbox)
    # with version=null so the indexer rejects it into the DLQ where it is visible.
    version_int = int(version) if isinstance(version, int | str) and str(version).isdigit() else None
    value = {
        "event_id": str(row["id"]),
        "event_type": row["event_type"],
        "sku": sku,
        "version": version_int,
        "outbox_created_at": iso_ms(row["created_at"]),
        "product": payload,
    }
    base: list[tuple[str, bytes]] = [
        ("event_id", str(row["id"]).encode()),
        ("sku", sku.encode()),
        ("version", str(version_int if version_int is not None else "").encode()),
        ("attempt", b"0"),
    ]
    carrier = _as_dict(row.get("trace_context")) or {}
    parent = context_from_carrier({str(k): str(v) for k, v in carrier.items()})
    # The producer span continues the feed-import trace; its context is what goes into traceparent.
    with _tracer.start_as_current_span(f"{topic} publish", context=parent, kind=SpanKind.PRODUCER) as span:
        span.set_attribute("messaging.system", "kafka")
        span.set_attribute("messaging.destination.name", topic)
        span.set_attribute("souq.outbox.id", int(row["id"]))
        span.set_attribute("souq.sku", sku)
        headers = inject_kafka_headers(base)
    body = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    return OutgoingMessage(topic, sku.encode("utf-8"), body, headers)


@dataclass
class RelayStats:
    started_at: float = field(default_factory=time.time)
    heartbeat: float = field(default_factory=time.monotonic)
    published: int = 0
    batches: int = 0
    failures: int = 0
    last_batch_rows: int = 0
    last_batch_ms: float = 0.0
    backlog: int | None = None
    oldest_unpublished_s: float | None = None
    last_error: str | None = None


class OutboxRelay:
    def __init__(
        self,
        connect: Callable[[], psycopg.Connection[Any]],
        producer: Any,
        *,
        topic: str = "catalog.changes",
        batch_size: int = 500,
        poll_interval_s: float = 0.1,
        delivery_timeout_s: float = 30.0,
        retention_s: float = 0.0,
        prune_interval_s: float = 600.0,
        backlog_interval_s: float = 10.0,
        heartbeat: Callable[[], None] | None = None,
    ) -> None:
        self._connect = connect
        self._producer = producer
        self.topic = topic
        self.batch_size = batch_size
        self.poll_interval_s = poll_interval_s
        self.delivery_timeout_s = delivery_timeout_s
        self.retention_s = retention_s
        self.prune_interval_s = prune_interval_s
        self.backlog_interval_s = backlog_interval_s
        self._heartbeat_hook = heartbeat
        self._conn: psycopg.Connection[Any] | None = None
        self._stop = threading.Event()
        self._next_prune = 0.0
        self._next_backlog = 0.0
        self.stats = RelayStats()

    def request_stop(self) -> None:
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def _connection(self) -> psycopg.Connection[Any]:
        if self._conn is None or self._conn.closed:
            self._conn = self._connect()
        return self._conn

    def _drop_connection(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(psycopg.Error):
                self._conn.close()
        self._conn = None

    def publish_batch(self) -> int:
        """Publish up to ``batch_size`` rows; returns how many were published (0 = outbox drained)."""
        started = time.perf_counter()
        conn = self._connection()
        # The 10/s poll would otherwise make every SELECT/UPDATE its own root trace; the useful spans are
        # the per-row producer spans, which continue the feed import's trace.
        with suppress_instrumentation(), conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            rows: Sequence[dict[str, Any]] = cur.execute(SELECT_BATCH, {"limit": self.batch_size}).fetchall()
            if not rows:
                return 0
            messages = [build_message(row, self.topic) for row in rows]
            # Raises on any failed/unacknowledged delivery -> transaction rolls back -> rows retried.
            produce_and_wait(self._producer, messages, self.delivery_timeout_s)
            cur.execute(MARK_PUBLISHED, {"ids": [row["id"] for row in rows]})
        self.stats.published += len(rows)
        self.stats.batches += 1
        self.stats.last_batch_rows = len(rows)
        self.stats.last_batch_ms = (time.perf_counter() - started) * 1000
        return len(rows)

    def _housekeeping(self) -> None:
        with suppress_instrumentation():
            self._housekeeping_queries()

    def _housekeeping_queries(self) -> None:
        now = time.monotonic()
        if now >= self._next_backlog:
            self._next_backlog = now + self.backlog_interval_s
            conn = self._connection()
            with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
                row = cur.execute(BACKLOG).fetchone()
            if row is not None:
                self.stats.backlog = int(row["n"])
                oldest = row["oldest"]
                self.stats.oldest_unpublished_s = (
                    max(0.0, (datetime.now(UTC) - oldest).total_seconds()) if oldest else None
                )
        if self.retention_s > 0 and now >= self._next_prune:
            self._next_prune = now + self.prune_interval_s
            conn = self._connection()
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(PRUNE, {"secs": self.retention_s})
                if cur.rowcount:
                    log.info("pruned %d published outbox rows", cur.rowcount)

    def run(self) -> None:
        backoff = 0.5
        log.info("relay started: topic=%s batch=%d", self.topic, self.batch_size)
        try:
            while not self._stop.is_set():
                self.stats.heartbeat = time.monotonic()
                if self._heartbeat_hook is not None:
                    self._heartbeat_hook()
                try:
                    published = self.publish_batch()
                    if published < self.batch_size:
                        self._housekeeping()
                    backoff = 0.5
                    self.stats.last_error = None
                except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
                    self._fail(exc)
                    self._drop_connection()
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, 10.0)
                    continue
                except Exception as exc:  # delivery failure, missing table, ...: back off and retry
                    self._fail(exc)
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, 10.0)
                    continue
                if published < self.batch_size:
                    # Drained: wait for the next poll. A full batch loops immediately.
                    self._stop.wait(self.poll_interval_s)
        finally:
            # The loop only exits between transactions, so nothing is half-published here.
            try:
                self._producer.flush(10)
            finally:
                self._drop_connection()
            log.info("relay stopped after publishing %d events", self.stats.published)

    def _fail(self, exc: Exception) -> None:
        self.stats.failures += 1
        self.stats.last_error = f"{exc.__class__.__name__}: {exc}"
        log.warning("relay iteration failed: %s", self.stats.last_error)

"""Kafka consumer loop: collect -> index/route -> acknowledge routed -> commit.

Offsets are committed manually and only for messages that were indexed into every write index or
durably routed to a retry topic / the DLQ, so a crash or shutdown at any point re-delivers instead of
dropping; re-delivery is harmless because indexing is idempotent (external versions, 409 = success).

Retry topics are consumed by the same loop. A retry message whose ``not_before`` is in the future
pauses its partition and seeks back to it; the partition is resumed when due. The poll loop never
sleeps on a retry message, so ``catalog.changes`` keeps flowing meanwhile.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from confluent_kafka import OFFSET_INVALID, KafkaError, KafkaException, Message, TopicPartition
from opentelemetry import trace
from opentelemetry.trace import SpanKind

from souq_common.telemetry import extract_kafka_context, metrics
from souq_indexer.aimd import AimdController
from souq_indexer.events import (
    H_NOT_BEFORE,
    H_ORIGINAL_TOPIC,
    Envelope,
    EventParseError,
    header_int,
    header_map,
    normalise_headers,
    parse_event,
    routed_headers,
)
from souq_indexer.kafka_io import OutgoingMessage, produce_and_wait
from souq_indexer.pipeline import BatchProcessor, BatchResult, ShutdownRequestedError
from souq_indexer.routing import RETRY_TOPICS, TOPIC_CHANGES, TOPIC_DLQ

log = logging.getLogger(__name__)
_tracer = trace.get_tracer("souq.indexer")

TP = tuple[str, int]
CONSUMED_TOPICS: tuple[str, ...] = (TOPIC_CHANGES, *RETRY_TOPICS)
_DEFAULT_RETRY_ATTEMPT = {topic: n + 1 for n, topic in enumerate(RETRY_TOPICS)}


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass
class ServiceStats:
    started_at: float = field(default_factory=time.time)
    heartbeat: float = field(default_factory=time.monotonic)
    batches: int = 0
    indexed: int = 0
    routed: dict[str, int] = field(default_factory=dict)
    rejections: int = 0
    commits_failed: int = 0
    batch_failures: int = 0
    last_batch_ms: float = 0.0
    lag: dict[str, int] = field(default_factory=dict)
    dlq_depth: int = 0
    write_indices: list[str] = field(default_factory=list)
    assigned: int = 0


class IndexerService:
    def __init__(
        self,
        consumer: Any,
        producer: Any,
        processor: BatchProcessor,
        aimd: AimdController,
        *,
        batch_linger_ms: float,
        lag_report_interval_s: float = 5.0,
        dlq_depth_interval_s: float = 30.0,
        resolver: Any = None,
        heartbeat: Callable[[], None] | None = None,
    ) -> None:
        self._consumer = consumer
        self._producer = producer
        self.processor = processor
        self.aimd = aimd
        self._linger_s = batch_linger_ms / 1000.0
        self._lag_interval = lag_report_interval_s
        self._dlq_interval = dlq_depth_interval_s
        self._resolver = resolver
        self._heartbeat_hook = heartbeat
        self._stop = threading.Event()
        self._assigned: set[TP] = set()
        self._retry_paused: dict[TP, int] = {}  # tp -> resume at (epoch ms)
        self._pending_seek: dict[TP, int] = {}
        self._committed: dict[TP, int] = {}
        self._next_lag_report = 0.0
        self._next_dlq_report = 0.0
        # Recent index_freshness samples for /stats (the histogram metric is the source of truth).
        self._freshness: deque[float] = deque(maxlen=20_000)
        self.stats = ServiceStats()

    # --------------------------------------------------------------- lifecycle
    def request_stop(self) -> None:
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def run(self) -> None:
        self._consumer.subscribe(
            list(CONSUMED_TOPICS), on_assign=self._on_assign, on_revoke=self._on_revoke, on_lost=self._on_lost
        )
        metrics().indexer_batch_size.set(self.aimd.batch_size)
        try:
            while not self._stop.is_set():
                self.run_once()
        finally:
            self._close()

    def _beat(self) -> None:
        self.stats.heartbeat = time.monotonic()
        if self._heartbeat_hook is not None:
            self._heartbeat_hook()

    def run_once(self) -> None:
        self._beat()
        if self._resolver is not None:
            self._resolver.maybe_refresh()
        self._resume_due()
        batch = self._collect()
        if batch:
            self._handle(batch)
        self._report_periodic()

    def _close(self) -> None:
        # Nothing un-indexed is ever committed: the in-flight batch either completed (and committed) or
        # was abandoned, and enable.auto.commit is off, so close() commits nothing on our behalf.
        try:
            self._producer.flush(10)
        finally:
            self._consumer.close()
        log.info("indexer stopped")

    # -------------------------------------------------------------- rebalance
    def _on_assign(self, consumer: Any, partitions: list[TopicPartition]) -> None:
        for p in partitions:
            tp = (p.topic, p.partition)
            self._assigned.add(tp)
            self._retry_paused.pop(tp, None)
            self._pending_seek.pop(tp, None)
            self._committed.pop(tp, None)
        self.stats.assigned = len(self._assigned)
        log.info("assigned %d partitions (total %d)", len(partitions), len(self._assigned))

    def _on_revoke(self, consumer: Any, partitions: list[TopicPartition]) -> None:
        for p in partitions:
            tp = (p.topic, p.partition)
            self._assigned.discard(tp)
            self._retry_paused.pop(tp, None)
            self._pending_seek.pop(tp, None)
            self._committed.pop(tp, None)
        self.stats.assigned = len(self._assigned)
        log.info("revoked %d partitions (total %d)", len(partitions), len(self._assigned))

    def _on_lost(self, consumer: Any, partitions: list[TopicPartition]) -> None:
        self._on_revoke(consumer, partitions)

    # ---------------------------------------------------------------- collect
    def _envelope(self, msg: Message) -> Envelope:
        headers = normalise_headers(msg.headers())
        meta = header_map(headers)
        topic = msg.topic() or ""
        if topic in RETRY_TOPICS:
            attempt = header_int(meta, "attempt", _DEFAULT_RETRY_ATTEMPT[topic])
            original_topic = meta.get(H_ORIGINAL_TOPIC, TOPIC_CHANGES)
        else:
            # A fresh (or redriven) change restarts the retry ladder.
            attempt = 0
            original_topic = topic
        env = Envelope(
            topic=topic,
            partition=msg.partition() or 0,
            offset=msg.offset() or 0,
            key=msg.key() if isinstance(msg.key(), bytes) else None,
            value=msg.value() if isinstance(msg.value(), bytes) else None,
            headers=headers,
            received_ns=time.time_ns(),
            attempt=attempt,
            original_topic=original_topic,
            meta=meta,
        )
        try:
            env.event = parse_event(env.value)
        except EventParseError as exc:
            env.parse_error = exc
        return env

    def _collect(self) -> list[Envelope]:
        target = max(1, self.aimd.batch_size)
        batch: list[Envelope] = []
        deadline: float | None = None
        while len(batch) < target and not self._stop.is_set():
            if deadline is None:
                timeout = 0.25
            else:
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    break
            msgs = self._consumer.consume(num_messages=min(target - len(batch), 1000), timeout=timeout)
            if not msgs and deadline is None:
                break  # idle: go do housekeeping (resume due retry partitions, lag report)
            now_ms = _now_ms()
            for msg in msgs:
                err = msg.error()
                if err is not None:
                    if err.code() == KafkaError._PARTITION_EOF:
                        continue
                    if err.fatal():
                        raise KafkaException(err)
                    log.warning("consumer error: %s", err)
                    continue
                tp = (msg.topic() or "", msg.partition() or 0)
                if tp in self._retry_paused:
                    continue  # pre-fetched past a not-yet-due message; re-read after resume
                if tp[0] in RETRY_TOPICS:
                    not_before = header_int(header_map(normalise_headers(msg.headers())), H_NOT_BEFORE, 0)
                    if not_before > now_ms:
                        self._pause_until(tp, msg.offset() or 0, not_before)
                        continue
                batch.append(self._envelope(msg))
                if deadline is None:
                    deadline = time.monotonic() + self._linger_s
        return batch

    # ------------------------------------------------------- retry-topic pause
    def _pause_until(self, tp: TP, offset: int, resume_at_ms: int) -> None:
        part = TopicPartition(tp[0], tp[1], offset)
        self._consumer.pause([part])
        self._retry_paused[tp] = resume_at_ms
        try:
            self._consumer.seek(part)
        except KafkaException:
            # Some client versions refuse to seek a paused partition; seek right after resume instead.
            self._pending_seek[tp] = offset
        log.debug("retry partition %s paused until %d (offset %d)", tp, resume_at_ms, offset)

    def _resume_due(self) -> None:
        if not self._retry_paused:
            return
        now_ms = _now_ms()
        for tp, resume_at in list(self._retry_paused.items()):
            if resume_at > now_ms:
                continue
            del self._retry_paused[tp]
            self._consumer.resume([TopicPartition(tp[0], tp[1])])
            offset = self._pending_seek.pop(tp, None)
            if offset is not None:
                self._consumer.seek(TopicPartition(tp[0], tp[1], offset))

    # ------------------------------------------------------------------- idle
    def idle(self, seconds: float) -> None:
        """Back off without leaving the group: pause everything and keep polling (no messages are
        delivered while paused, but rebalances and max.poll.interval.ms are serviced)."""
        deadline = time.monotonic() + max(0.0, seconds)
        assignment = list(self._consumer.assignment())
        if assignment:
            self._consumer.pause(assignment)
        try:
            while not self._stop.is_set():
                self._beat()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                msg = self._consumer.poll(min(0.25, remaining))
                if msg is not None and msg.error() is None:
                    # From a partition assigned during the backoff (not paused yet): rewind and pause it.
                    part = TopicPartition(msg.topic() or "", msg.partition() or 0, msg.offset() or 0)
                    self._consumer.pause([part])
                    try:
                        self._consumer.seek(part)
                    except KafkaException:
                        self._pending_seek[(part.topic, part.partition)] = part.offset
        finally:
            resume = [
                p for p in self._consumer.assignment() if (p.topic, p.partition) not in self._retry_paused
            ]
            if resume:
                self._consumer.resume(resume)
            for tp, offset in list(self._pending_seek.items()):
                if tp not in self._retry_paused:
                    del self._pending_seek[tp]
                    self._consumer.seek(TopicPartition(tp[0], tp[1], offset))

    # ----------------------------------------------------------------- handle
    def _rewind(self, batch: Sequence[Envelope]) -> None:
        """Seek each partition back to its first uncommitted message in ``batch`` so it is re-consumed."""
        first: dict[TP, int] = {}
        for env in batch:
            tp = (env.topic, env.partition)
            first[tp] = min(first.get(tp, env.offset), env.offset)
        for tp, offset in first.items():
            if tp not in self._assigned:
                continue
            if tp in self._pending_seek:
                self._pending_seek[tp] = min(self._pending_seek[tp], offset)
            try:
                self._consumer.seek(TopicPartition(tp[0], tp[1], offset))
            except KafkaException as exc:
                log.warning("seek %s@%d failed: %s", tp, offset, exc)

    def _handle(self, batch: list[Envelope]) -> None:
        started = time.perf_counter()
        try:
            result = self.processor.process(batch)
            self._acknowledge_routed(result)
        except ShutdownRequestedError:
            log.info("shutdown during batch of %d: abandoning it uncommitted", len(batch))
            return
        except Exception as exc:  # DeliveryError, embedder failure, ...: retry the whole batch
            self.stats.batch_failures += 1
            log.exception("batch of %d failed, will be re-consumed: %s", len(batch), exc)
            self._rewind(batch)
            self.idle(self.aimd.backoff_seconds(min(self.stats.batch_failures, 6)))
            return
        self.stats.batch_failures = 0
        self._commit(batch)
        self._trace_messages(batch, result)
        self.stats.batches += 1
        self.stats.indexed += result.indexed
        self._freshness.extend(result.freshness_s)
        self.stats.rejections += result.rejections
        self.stats.last_batch_ms = (time.perf_counter() - started) * 1000
        for routed in result.routed:
            self.stats.routed[routed.route.topic] = self.stats.routed.get(routed.route.topic, 0) + 1
        if result.routed:
            log.warning(
                "batch: %d indexed, %d routed (%s)",
                result.indexed,
                len(result.routed),
                ", ".join(sorted({r.route.topic for r in result.routed})),
            )

    def _acknowledge_routed(self, result: BatchResult) -> None:
        outgoing = [
            OutgoingMessage(
                topic=r.route.topic,
                key=r.envelope.key,
                value=r.envelope.value,
                headers=routed_headers(
                    r.envelope.headers,
                    attempt=r.route.attempt,
                    not_before_ms=r.route.not_before_ms,
                    original_topic=r.original_topic,
                    error_class=r.error_class,
                    error_message=r.error_message,
                ),
            )
            for r in result.routed
        ]
        produce_and_wait(self._producer, outgoing)
        if any(r.route.topic == TOPIC_DLQ for r in result.routed):
            self._next_dlq_report = 0.0  # refresh dlq_depth promptly

    def _commit(self, batch: Sequence[Envelope]) -> None:
        last: dict[TP, int] = {}
        for env in batch:
            tp = (env.topic, env.partition)
            last[tp] = max(last.get(tp, -1), env.offset)
        offsets = [TopicPartition(t, p, o + 1) for (t, p), o in last.items() if (t, p) in self._assigned]
        if not offsets:
            return
        try:
            self._consumer.commit(offsets=offsets, asynchronous=False)
        except KafkaException as exc:
            # Typically a rebalance in flight; the new owner re-processes (idempotently).
            self.stats.commits_failed += 1
            log.warning("offset commit failed: %s", exc)
            return
        for part in offsets:
            self._committed[(part.topic, part.partition)] = part.offset

    def _trace_messages(self, batch: Sequence[Envelope], result: BatchResult) -> None:
        routed = {(r.envelope.topic, r.envelope.partition, r.envelope.offset): r for r in result.routed}
        for env in batch:
            span = _tracer.start_span(
                f"{env.topic} process",
                context=extract_kafka_context(env.headers),
                kind=SpanKind.CONSUMER,
                start_time=env.received_ns,
            )
            span.set_attribute("messaging.system", "kafka")
            span.set_attribute("messaging.destination.name", env.topic)
            span.set_attribute("messaging.kafka.partition", env.partition)
            span.set_attribute("messaging.kafka.offset", env.offset)
            span.set_attribute("souq.sku", env.sku)
            r = routed.get((env.topic, env.partition, env.offset))
            span.set_attribute("souq.indexer.outcome", "indexed" if r is None else f"routed:{r.route.topic}")
            span.end()

    # -------------------------------------------------------------- reporting
    def freshness_summary(self) -> dict[str, float | int]:
        samples = sorted(self._freshness)
        if not samples:
            return {"n": 0}

        def pct(q: float) -> float:
            return round(samples[min(len(samples) - 1, int(q * len(samples)))], 3)

        return {
            "n": len(samples),
            "p50_s": pct(0.50),
            "p95_s": pct(0.95),
            "p99_s": pct(0.99),
            "max_s": samples[-1],
        }

    def _report_periodic(self) -> None:
        now = time.monotonic()
        if now >= self._next_lag_report:
            self._next_lag_report = now + self._lag_interval
            try:
                self.report_lag()
            except KafkaException as exc:
                log.warning("lag report failed: %s", exc)
        if now >= self._next_dlq_report:
            self._next_dlq_report = now + self._dlq_interval
            try:
                self.report_dlq_depth()
            except KafkaException as exc:
                log.warning("dlq depth report failed: %s", exc)
        if self._resolver is not None:
            try:
                self.stats.write_indices = list(self._resolver.indices())
            except Exception:
                self.stats.write_indices = []

    def report_lag(self) -> dict[str, int]:
        parts = [TopicPartition(t, p) for t, p in sorted(self._assigned)]
        missing = [p for p in parts if (p.topic, p.partition) not in self._committed]
        if missing:
            for c in self._consumer.committed(missing, timeout=5):
                self._committed[(c.topic, c.partition)] = c.offset
        lag: dict[str, int] = {}
        gauge = metrics().consumer_lag
        for part in parts:
            low, high = self._consumer.get_watermark_offsets(part, timeout=2, cached=False)
            committed = self._committed.get((part.topic, part.partition), OFFSET_INVALID)
            value = max(0, high - (committed if committed >= 0 else low))
            lag[f"{part.topic}/{part.partition}"] = value
            gauge.set(value, {"topic": part.topic, "partition": part.partition})
        self.stats.lag = lag
        return lag

    def report_dlq_depth(self) -> int:
        meta = self._consumer.list_topics(TOPIC_DLQ, timeout=5)
        topic = meta.topics.get(TOPIC_DLQ)
        if topic is None or topic.error is not None:
            return self.stats.dlq_depth
        depth = 0
        for pid in topic.partitions:
            low, high = self._consumer.get_watermark_offsets(
                TopicPartition(TOPIC_DLQ, pid), timeout=2, cached=False
            )
            depth += max(0, high - low)
        self.stats.dlq_depth = depth
        metrics().dlq_depth.set(depth)
        return depth

"""One consumed batch -> OpenSearch bulk(s), with per-item retry, AIMD backpressure and routing.

Failure taxonomy (see ``routing``):

* ``ok`` / ``conflict_ok`` — done. A 409 under external versioning means the index already holds this
  version or a newer one, so duplicates and out-of-order events are no-ops instead of regressions.
* ``non_retryable`` (mapping/parse errors) — straight to ``catalog.dlq``; retrying cannot help.
* retryable *backpressure* (429, thread-pool rejections, cluster unavailable, connection refused) — the
  cluster is the problem, not the message: halve the batch, back off exponentially and retry the same
  items without spending their attempt budget. An outage therefore stalls the partition (lag grows,
  nothing is committed) instead of draining it into the DLQ.
* other retryable errors — up to ``in_process_attempts`` tries, then one hop down the retry ladder.

The batch is only reported as handled once every message is either indexed into every write index or
routed; the caller commits offsets after routed messages are acknowledged by Kafka.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any

from opensearchpy.exceptions import ConnectionError as OSConnectionError
from opensearchpy.exceptions import TransportError

from souq_common.embeddings import Embedder
from souq_common.telemetry import batch_span, metrics
from souq_indexer.aimd import AimdController
from souq_indexer.aliases import NoWriteIndexError, WriteAliasResolver
from souq_indexer.documents import attach_embeddings, build_document
from souq_indexer.events import ChangeEvent, Envelope
from souq_indexer.routing import (
    TOPIC_CHANGES,
    Route,
    classify_item_error,
    is_backpressure,
    is_rejection,
    next_route,
)

log = logging.getLogger(__name__)


class ShutdownRequestedError(Exception):
    """Raised mid-batch on shutdown: the batch is abandoned uncommitted and re-read on restart."""


@dataclass(slots=True)
class RoutedMessage:
    envelope: Envelope
    route: Route
    error_class: str
    error_message: str
    original_topic: str = TOPIC_CHANGES


@dataclass(slots=True)
class BatchResult:
    indexed: int = 0
    routed: list[RoutedMessage] = field(default_factory=list)
    rejections: int = 0
    bulk_requests: int = 0
    rounds: int = 0
    freshness_s: list[float] = field(default_factory=list)


@dataclass(slots=True)
class _Failure:
    status: int
    error_type: str | None
    reason: str


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _dumps(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _transport_failure(exc: Exception) -> _Failure:
    """Map a whole-request failure onto the per-item taxonomy."""
    if isinstance(exc, OSConnectionError):
        # Connection refused / timed out: the cluster is unreachable — treat like 503.
        return _Failure(503, "connection_error", str(exc) or exc.__class__.__name__)
    if isinstance(exc, TransportError):
        status = exc.status_code if isinstance(exc.status_code, int) else 503
        error_type: str | None = None
        info = exc.info if isinstance(exc.info, dict) else {}
        err = info.get("error")
        if isinstance(err, dict):
            error_type = err.get("type")
        elif isinstance(exc.error, str) and exc.error:
            error_type = exc.error
        if status == 413:
            # Request too large: same remedy as a rejection — shrink the batch.
            return _Failure(429, "request_entity_too_large", str(exc))
        return _Failure(status, error_type, str(exc))
    return _Failure(500, exc.__class__.__name__, str(exc))


class BatchProcessor:
    def __init__(
        self,
        client: Any,
        resolver: WriteAliasResolver,
        embedder: Embedder,
        aimd: AimdController,
        *,
        in_process_attempts: int = 3,
        bulk_timeout_s: float = 30.0,
        max_bulk_bytes: int = 10 * 1024 * 1024,
        embed_batch: int = 256,
        idle: Callable[[float], None] = time.sleep,
        should_stop: Callable[[], bool] = lambda: False,
        now_ms: Callable[[], int] = _now_ms,
    ) -> None:
        self._client = client
        self._resolver = resolver
        self._embedder = embedder
        self._aimd = aimd
        self._attempts = max(1, in_process_attempts)
        self._bulk_timeout_s = bulk_timeout_s
        self._max_bulk_bytes = max_bulk_bytes
        self._embed_batch = embed_batch
        self._idle = idle
        self._should_stop = should_stop
        self._now_ms = now_ms
        self.backpressure_active = False

    # ------------------------------------------------------------------ public
    def process(self, envelopes: Sequence[Envelope]) -> BatchResult:
        result = BatchResult()
        # One span per batch, linked to every message's producer context (feeds -> relay -> here).
        with batch_span("indexer.bulk_batch", [env.headers for env in envelopes]) as span:
            self._process(envelopes, result)
            span.set_attribute("souq.indexer.indexed", result.indexed)
            span.set_attribute("souq.indexer.routed", len(result.routed))
            span.set_attribute("souq.indexer.rejections", result.rejections)
            span.set_attribute("souq.indexer.bulk_requests", result.bulk_requests)
        docs = metrics().indexer_docs_total
        if result.indexed:
            docs.add(result.indexed, {"outcome": "indexed"})
        for routed in result.routed:
            docs.add(1, {"outcome": routed.route.topic})
        return result

    # ----------------------------------------------------------------- private
    def _route(
        self, result: BatchResult, env: Envelope, retryable: bool, error_class: str, message: str
    ) -> None:
        route = next_route(env.attempt, retryable, self._now_ms())
        result.routed.append(
            RoutedMessage(env, route, error_class, message, env.original_topic or TOPIC_CHANGES)
        )

    def _process(self, envelopes: Sequence[Envelope], result: BatchResult) -> None:
        live: list[int] = []
        for i, env in enumerate(envelopes):
            if env.parse_error is not None:
                self._route(result, env, False, env.parse_error.error_class, env.parse_error.message)
            else:
                live.append(i)
        if not live:
            return

        sources: dict[int, bytes] = {}
        upserts = [i for i in live if _event(envelopes[i]).event_type == "product.upserted"]
        docs = {i: build_document(_event(envelopes[i])) for i in upserts}
        if docs:
            attach_embeddings(list(docs.values()), self._embedder, self._embed_batch)
        for i, doc in docs.items():
            sources[i] = _dumps(doc)

        indices = self._wait_for_indices()
        pending: dict[int, set[str]] = {i: set(indices) for i in live}
        attempts: dict[int, int] = {}
        backpressure_rounds = 0
        failed_rounds = 0
        rejected_in_batch = False

        while pending:
            if self._should_stop():
                raise ShutdownRequestedError
            result.rounds += 1
            current = set(self._wait_for_indices())
            # An index dropped from the alias mid-batch no longer needs this change.
            for i in list(pending):
                pending[i] &= current
                if not pending[i]:
                    del pending[i]
                    self._succeeded(result, envelopes[i])
            if not pending:
                break

            counted: dict[int, _Failure] = {}
            backpressure = False
            rejected = 0
            items = [(i, idx) for i in sorted(pending) for idx in sorted(pending[i])]
            for chunk, body in self._chunks(items, envelopes, sources):
                result.bulk_requests += 1
                outcomes = self._send(chunk, body)
                for (i, idx), failure in zip(chunk, outcomes, strict=True):
                    if i not in pending:
                        continue  # already routed by a failure on another index in this round
                    if failure is None:
                        pending[i].discard(idx)
                        if not pending[i]:
                            del pending[i]
                            counted.pop(i, None)
                            self._succeeded(result, envelopes[i])
                        continue
                    kind = classify_item_error(failure.status, failure.error_type)
                    if kind in ("ok", "conflict_ok"):  # _send already filters these; defensive
                        continue
                    if failure.error_type == "index_not_found_exception":
                        self._resolver.invalidate()
                    if kind == "non_retryable":
                        del pending[i]
                        counted.pop(i, None)
                        self._route(
                            result,
                            envelopes[i],
                            False,
                            failure.error_type or f"http_{failure.status}",
                            f"[{idx}] {failure.reason}",
                        )
                    elif is_backpressure(failure.status, failure.error_type):
                        backpressure = True
                        if is_rejection(failure.status, failure.error_type):
                            rejected += 1
                    else:
                        counted[i] = _Failure(failure.status, failure.error_type, f"[{idx}] {failure.reason}")

            for i, failure in counted.items():
                if i not in pending:
                    continue
                attempts[i] = attempts.get(i, 0) + 1
                if attempts[i] >= self._attempts:
                    del pending[i]
                    self._route(
                        result,
                        envelopes[i],
                        True,
                        failure.error_type or f"http_{failure.status}",
                        f"{failure.reason} (after {attempts[i]} in-process attempts)",
                    )

            if rejected:
                rejected_in_batch = True
                result.rejections += rejected
                self._aimd.on_rejection()
                metrics().bulk_rejections_total.add(rejected)
                metrics().indexer_batch_size.set(self._aimd.batch_size)

            if pending:
                if backpressure:
                    backpressure_rounds += 1
                    delay = self._aimd.backoff_seconds(backpressure_rounds)
                    self.backpressure_active = True
                    log.warning(
                        "backpressure: %d messages pending, batch_size=%d, backing off %.1fs",
                        len(pending),
                        self._aimd.batch_size,
                        delay,
                    )
                else:
                    failed_rounds += 1
                    delay = self._aimd.backoff_seconds(failed_rounds)
                self._idle(delay)

        self.backpressure_active = False
        if not rejected_in_batch:
            self._aimd.on_success()
            metrics().indexer_batch_size.set(self._aimd.batch_size)

    def _succeeded(self, result: BatchResult, env: Envelope) -> None:
        result.indexed += 1
        created = _event(env).outbox_created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        freshness = max(0.0, self._now_ms() / 1000.0 - created.timestamp())
        result.freshness_s.append(freshness)
        metrics().index_freshness_seconds.record(freshness)

    def _wait_for_indices(self) -> tuple[str, ...]:
        """Until the write alias exists, behave as during an outage: back off, don't route anything."""
        rounds = 0
        while True:
            try:
                return self._resolver.indices()
            except NoWriteIndexError as exc:
                if self._should_stop():
                    raise ShutdownRequestedError from exc
                rounds += 1
                self.backpressure_active = True
                delay = self._aimd.backoff_seconds(rounds)
                log.error("%s; retrying in %.1fs", exc, delay)
                self._idle(delay)
                self._resolver.invalidate()

    def _chunks(
        self, items: list[tuple[int, str]], envelopes: Sequence[Envelope], sources: dict[int, bytes]
    ) -> Iterator[tuple[list[tuple[int, str]], bytes]]:
        """Split into bulk requests bounded by the AIMD batch size (actions) and by bytes."""
        limit = max(1, self._aimd.batch_size)
        chunk: list[tuple[int, str]] = []
        parts: list[bytes] = []
        size = 0
        for i, idx in items:
            event = _event(envelopes[i])
            op = "index" if event.event_type == "product.upserted" else "delete"
            meta = {
                op: {"_index": idx, "_id": event.sku, "version": event.version, "version_type": "external"}
            }
            line = _dumps(meta) + b"\n"
            if op == "index":
                line += sources[i] + b"\n"
            if chunk and (len(chunk) >= limit or size + len(line) > self._max_bulk_bytes):
                yield chunk, b"".join(parts)
                chunk, parts, size = [], [], 0
            chunk.append((i, idx))
            parts.append(line)
            size += len(line)
        if chunk:
            yield chunk, b"".join(parts)

    def _send(self, chunk: list[tuple[int, str]], body: bytes) -> list[_Failure | None]:
        """Execute one bulk request; return ``None`` for each item that is done (ok or 409)."""
        try:
            resp = self._client.bulk(
                body=body,
                request_timeout=self._bulk_timeout_s,
                timeout=f"{int(self._bulk_timeout_s * 1000)}ms",
                filter_path="errors,items.*.status,items.*.error.type,items.*.error.reason",
            )
        except Exception as exc:
            failure = _transport_failure(exc)
            log.warning("bulk request failed (%s): %s", failure.error_type, failure.reason[:300])
            return [failure] * len(chunk)

        if not resp.get("errors"):
            return [None] * len(chunk)
        items = resp.get("items") or []
        if len(items) != len(chunk):
            # Should never happen; refuse to guess which item is which.
            failure = _Failure(500, "bulk_response_mismatch", f"{len(items)} items for {len(chunk)} actions")
            return [failure] * len(chunk)
        out: list[_Failure | None] = []
        for item in items:
            op = next(iter(item.values()))
            status = int(op.get("status", 500))
            err = op.get("error")
            error_type = err.get("type") if isinstance(err, dict) else None
            if classify_item_error(status, error_type) in ("ok", "conflict_ok"):
                out.append(None)
                continue
            reason = err.get("reason", "") if isinstance(err, dict) else str(err or "")
            out.append(_Failure(status, error_type, str(reason)))
        return out


def _event(env: Envelope) -> ChangeEvent:
    if env.event is None:  # parse failures are routed before any indexing work
        raise RuntimeError(f"envelope {env.topic}/{env.partition}@{env.offset} has no parsed event")
    return env.event

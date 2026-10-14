"""Per-item bulk outcome classification and retry-topic routing.

``attempt`` is the value of the ``attempt`` header on the consumed message: 0 on ``catalog.changes``,
1 on ``catalog.retry.30s``, 2 on ``catalog.retry.5m``. A message that still fails after the in-process
attempts moves one hop down the ladder; non-retryable failures skip the ladder and go to the DLQ.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from souq_common.budgets import load_budgets

Outcome = Literal["ok", "conflict_ok", "retryable", "non_retryable"]

TOPIC_CHANGES = "catalog.changes"
TOPIC_RETRY_30S = "catalog.retry.30s"
TOPIC_RETRY_5M = "catalog.retry.5m"
TOPIC_DLQ = "catalog.dlq"
RETRY_TOPICS: tuple[str, ...] = (TOPIC_RETRY_30S, TOPIC_RETRY_5M)

# Transient server-side conditions: the same request can succeed later without any change to the document.
_RETRYABLE_TYPES = frozenset(
    {
        "es_rejected_execution_exception",
        "rejected_execution_exception",
        "circuit_breaking_exception",
        "cluster_block_exception",
        "unavailable_shards_exception",
        "no_shard_available_action_exception",
        "primary_missing_action_exception",
        "node_not_connected_exception",
        "node_closed_exception",
        "process_cluster_event_timeout_exception",
        "timeout_exception",
        "receive_timeout_transport_exception",
        "index_not_found_exception",  # stale products_write alias; fixed by the next alias refresh
        "shard_not_found_exception",
    }
)

# Cluster overload / unavailability: handled with backpressure (pause + AIMD + backoff) rather than by
# spending the message's retry budget, so an outage never drains catalog.changes into the DLQ.
_BACKPRESSURE_TYPES = frozenset(
    {
        "es_rejected_execution_exception",
        "rejected_execution_exception",
        "circuit_breaking_exception",
        "cluster_block_exception",
        "unavailable_shards_exception",
        "no_shard_available_action_exception",
        "primary_missing_action_exception",
        "node_not_connected_exception",
        "node_closed_exception",
    }
)

_REJECTION_TYPES = frozenset({"es_rejected_execution_exception", "rejected_execution_exception"})


def classify_item_error(status: int, error_type: str | None) -> Outcome:
    if 200 <= status < 300:
        return "ok"
    if status == 404 and error_type is None:
        # Delete of a document that is not there: the external-version tombstone is still written.
        return "ok"
    if status == 409 or error_type == "version_conflict_engine_exception":
        # External versioning: the index already holds this version or a newer one.
        return "conflict_ok"
    if status == 429 or status >= 500 or (error_type is not None and error_type in _RETRYABLE_TYPES):
        return "retryable"
    return "non_retryable"


def is_rejection(status: int, error_type: str | None) -> bool:
    """429 / thread-pool rejection — the signal AIMD reacts to."""
    return status == 429 or (error_type is not None and error_type in _REJECTION_TYPES)


def is_backpressure(status: int, error_type: str | None) -> bool:
    """Retryable condition caused by cluster health/load rather than by the message itself."""
    if is_rejection(status, error_type):
        return True
    return status in (502, 503, 504) or (error_type is not None and error_type in _BACKPRESSURE_TYPES)


@dataclass(frozen=True, slots=True)
class Route:
    topic: str
    not_before_ms: int | None
    attempt: int


def _retry_delays_ms() -> tuple[int, int]:
    delays = [int(float(s) * 1000) for s in load_budgets().indexer.retry_delays_s]
    if len(delays) != len(RETRY_TOPICS):
        raise ValueError("indexer.retry_delays_s must have one entry per retry topic")
    return delays[0], delays[1]


def next_route(attempt: int, retryable: bool, now_ms: int) -> Route:
    """Where a message that failed processing at ``attempt`` goes next."""
    attempt = max(0, attempt)
    if retryable and attempt < len(RETRY_TOPICS):
        delay_ms = _retry_delays_ms()[attempt]
        return Route(RETRY_TOPICS[attempt], now_ms + delay_ms, attempt + 1)
    return Route(TOPIC_DLQ, None, attempt + 1)

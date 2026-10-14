"""Bulk item classification and the retry ladder.

Ladder (CLAUDE.md + config/budgets.yaml ``indexer``): in-process attempts 1..N on the consumed message →
``catalog.retry.30s`` → ``catalog.retry.5m`` → ``catalog.dlq``; non-retryable failures go straight to the
DLQ. ``next_route``'s ``attempt`` is the message's ``attempt`` header (0 on catalog.changes, 1 on
retry.30s, 2 on retry.5m). Delays and N are read from budgets rather than restated as literals.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from souq_common.budgets import load_budgets
from souq_common.embeddings import FakeEmbedder
from souq_indexer.aimd import AimdController
from souq_indexer.events import ChangeEvent, Envelope
from souq_indexer.pipeline import BatchProcessor, BatchResult
from souq_indexer.routing import classify_item_error, next_route

NOW = 1_760_000_000_000
BUDGETS = load_budgets().indexer
IN_PROCESS = int(BUDGETS.in_process_attempts)
DELAY_30S_MS, DELAY_5M_MS = (int(s) * 1000 for s in BUDGETS.retry_delays_s)

CHANGES = "catalog.changes"
RETRY_30S = "catalog.retry.30s"
RETRY_5M = "catalog.retry.5m"
DLQ = "catalog.dlq"


# --------------------------------------------------------------------------- classify_item_error


@pytest.mark.parametrize(
    ("status", "error_type", "expected"),
    [
        (200, None, "ok"),
        (201, None, "ok"),
        # external versioning: an older/equal version arriving late is a success, not a failure
        (409, "version_conflict_engine_exception", "conflict_ok"),
        (409, None, "conflict_ok"),
        # backpressure
        (429, "es_rejected_execution_exception", "retryable"),
        (429, "circuit_breaking_exception", "retryable"),
        (429, None, "retryable"),
        # cluster-side transient
        (500, None, "retryable"),
        (502, None, "retryable"),
        (503, "unavailable_shards_exception", "retryable"),
        (504, None, "retryable"),
        # the document itself is bad: retrying cannot help
        (400, "mapper_parsing_exception", "non_retryable"),
        (400, "document_parsing_exception", "non_retryable"),
        (400, "illegal_argument_exception", "non_retryable"),
        (400, "strict_dynamic_mapping_exception", "non_retryable"),
    ],
)
def test_classify_item_error(status: int, error_type: str | None, expected: str) -> None:
    assert classify_item_error(status, error_type) == expected


# --------------------------------------------------------------------------- next_route


def test_budget_ladder_matches_contract() -> None:
    assert IN_PROCESS == 3
    assert (DELAY_30S_MS, DELAY_5M_MS) == (30_000, 300_000)


def test_failure_on_changes_goes_to_retry_30s() -> None:
    r = next_route(0, True, NOW)
    assert (r.topic, r.not_before_ms, r.attempt) == (RETRY_30S, NOW + DELAY_30S_MS, 1)


def test_failure_on_retry_30s_goes_to_retry_5m() -> None:
    r = next_route(1, True, NOW)
    assert (r.topic, r.not_before_ms, r.attempt) == (RETRY_5M, NOW + DELAY_5M_MS, 2)


def test_failure_on_retry_5m_goes_to_dlq() -> None:
    r = next_route(2, True, NOW)
    assert r.topic == DLQ
    assert r.attempt == 3


@pytest.mark.parametrize("attempt", [0, 1, 2, 7])
def test_non_retryable_goes_to_dlq_immediately(attempt: int) -> None:
    assert next_route(attempt, False, NOW).topic == DLQ


@pytest.mark.parametrize("attempt", [3, 4, 50])
def test_anything_past_the_ladder_is_dead(attempt: int) -> None:
    assert next_route(attempt, True, NOW).topic == DLQ


def test_full_ladder_walk() -> None:
    """Follow a permanently failing retryable event from catalog.changes to the DLQ."""
    topics: list[str] = []
    attempt = 0
    for _ in range(10):
        r = next_route(attempt, True, NOW)
        topics.append(r.topic)
        if r.topic == DLQ:
            break
        assert r.attempt == attempt + 1
        attempt = r.attempt
    assert topics == [RETRY_30S, RETRY_5M, DLQ]


def test_not_before_is_monotonic_along_the_ladder() -> None:
    r30 = next_route(0, True, NOW)
    r5m = next_route(1, True, NOW)
    assert r30.not_before_ms is not None
    assert r5m.not_before_ms is not None
    assert r5m.not_before_ms > r30.not_before_ms > NOW


# --------------------------------------------------------------------------- in-process attempts (pipeline)

Script = Callable[[int, list[dict[str, Any]]], list[tuple[int, str | None]]]


class FakeBulkClient:
    """Answers each bulk call from a script: (call number, actions) -> per-item (status, error type)."""

    def __init__(self, script: Script) -> None:
        self.script = script
        self.calls: list[list[dict[str, Any]]] = []

    def bulk(self, body: bytes, **_: Any) -> dict[str, Any]:
        lines = [json.loads(line) for line in body.decode().splitlines() if line]
        actions = [line for line in lines if set(line) & {"index", "delete"}]
        self.calls.append(actions)
        outcomes = self.script(len(self.calls), actions)
        items = []
        for action, (status, error_type) in zip(actions, outcomes, strict=True):
            op = next(iter(action))
            item: dict[str, Any] = {"status": status}
            if error_type is not None:
                item["error"] = {"type": error_type, "reason": f"scripted {error_type}"}
            items.append({op: item})
        return {"errors": any(status >= 300 for status, _ in outcomes), "items": items}


class StaticResolver:
    def __init__(self, *indices: str) -> None:
        self._indices = indices or ("products_v1",)

    def indices(self) -> tuple[str, ...]:
        return self._indices

    def invalidate(self) -> None:
        pass


def envelope(n: int, attempt: int = 0, topic: str = CHANGES) -> Envelope:
    sku = f"SKU{n:05d}"
    payload = {
        "event_id": str(n),
        "event_type": "product.upserted",
        "sku": sku,
        "version": 3,
        "outbox_created_at": datetime.now(UTC).isoformat(),
        "product": {
            "sku": sku,
            "brand": "sony",
            "title_en": f"headphones {n}",
            "price": 10.0,
            "currency": "AED",
        },
    }
    value = json.dumps(payload).encode()
    return Envelope(
        topic=topic,
        partition=0,
        offset=n,
        key=sku.encode(),
        value=value,
        headers=[],
        received_ns=0,
        attempt=attempt,
        original_topic=CHANGES,
        event=ChangeEvent.model_validate(payload),
    )


def run(
    script: Script, envelopes: list[Envelope], aimd: AimdController | None = None
) -> tuple[BatchResult, FakeBulkClient, list[float]]:
    client = FakeBulkClient(script)
    sleeps: list[float] = []
    processor = BatchProcessor(
        client,
        StaticResolver(),  # type: ignore[arg-type]
        FakeEmbedder(),
        aimd or AimdController(),
        in_process_attempts=IN_PROCESS,
        idle=sleeps.append,
        now_ms=lambda: NOW,
    )
    return processor.process(envelopes), client, sleeps


def always(status: int, error_type: str | None) -> Script:
    return lambda _call, actions: [(status, error_type)] * len(actions)


def test_transient_error_uses_every_in_process_attempt_then_retry_30s() -> None:
    result, client, sleeps = run(always(500, "timeout_exception"), [envelope(1)])
    assert len(client.calls) == IN_PROCESS
    assert result.indexed == 0
    assert len(result.routed) == 1
    route = result.routed[0].route
    assert (route.topic, route.not_before_ms, route.attempt) == (RETRY_30S, NOW + DELAY_30S_MS, 1)
    assert result.routed[0].error_class == "timeout_exception"
    # It backs off between in-process attempts, not after the last one.
    assert len(sleeps) == IN_PROCESS - 1


def test_transient_error_recovering_on_last_in_process_attempt_is_indexed() -> None:
    def script(call: int, actions: list[dict[str, Any]]) -> list[tuple[int, str | None]]:
        return [(500, "timeout_exception") if call < IN_PROCESS else (201, None)] * len(actions)

    result, client, _ = run(script, [envelope(1)])
    assert len(client.calls) == IN_PROCESS
    assert result.indexed == 1
    assert result.routed == []


@pytest.mark.parametrize(("attempt", "topic", "expected"), [(1, RETRY_30S, RETRY_5M), (2, RETRY_5M, DLQ)])
def test_retry_topic_messages_move_one_hop_down(attempt: int, topic: str, expected: str) -> None:
    result, client, _ = run(always(500, "timeout_exception"), [envelope(1, attempt=attempt, topic=topic)])
    assert len(client.calls) == IN_PROCESS
    assert [r.route.topic for r in result.routed] == [expected]
    assert result.routed[0].original_topic == CHANGES


def test_mapper_parsing_exception_goes_to_dlq_without_retrying() -> None:
    result, client, sleeps = run(always(400, "mapper_parsing_exception"), [envelope(1)])
    assert len(client.calls) == 1
    assert [r.route.topic for r in result.routed] == [DLQ]
    assert result.routed[0].error_class == "mapper_parsing_exception"
    assert sleeps == []


def test_version_conflict_is_success() -> None:
    result, client, _ = run(always(409, "version_conflict_engine_exception"), [envelope(1), envelope(2)])
    assert len(client.calls) == 1
    assert result.indexed == 2
    assert result.routed == []


def test_rejections_back_off_and_shrink_without_spending_attempts() -> None:
    # Rejected for more rounds than the in-process budget: backpressure must not route anything.
    rejected_rounds = IN_PROCESS + 2

    def script(call: int, actions: list[dict[str, Any]]) -> list[tuple[int, str | None]]:
        if call <= rejected_rounds:
            return [(429, "es_rejected_execution_exception")] * len(actions)
        return [(201, None)] * len(actions)

    aimd = AimdController(initial=1000)
    result, client, sleeps = run(script, [envelope(1), envelope(2)], aimd)
    assert result.routed == []
    assert result.indexed == 2
    assert len(client.calls) == rejected_rounds + 1
    assert result.rejections == 2 * rejected_rounds
    assert aimd.batch_size < 1000
    assert sleeps == sorted(sleeps)
    assert len(sleeps) == rejected_rounds


def test_mixed_batch_routes_only_the_failing_item() -> None:
    def script(_call: int, actions: list[dict[str, Any]]) -> list[tuple[int, str | None]]:
        return [
            (400, "mapper_parsing_exception") if a["index"]["_id"] == "SKU00002" else (201, None)
            for a in actions
        ]

    result, _, _ = run(script, [envelope(1), envelope(2), envelope(3)])
    assert result.indexed == 2
    assert [(r.envelope.sku, r.route.topic) for r in result.routed] == [("SKU00002", DLQ)]


def test_bulk_uses_external_versioning() -> None:
    _, client, _ = run(always(201, None), [envelope(1)])
    meta = client.calls[0][0]["index"]
    assert meta["version_type"] == "external"
    assert meta["version"] == 3
    assert meta["_id"] == "SKU00001"

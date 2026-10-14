"""BatchProcessor: per-item retries, AIMD backpressure, routing, idempotency, dual-write."""

from __future__ import annotations

import pytest
from indexer_fakes import NOW_MS, FakeOpenSearch, fail_items, make_env
from opensearchpy.exceptions import ConnectionError as OSConnectionError
from opensearchpy.exceptions import TransportError

from souq_common.embeddings import FakeEmbedder
from souq_indexer.aimd import AimdController
from souq_indexer.aliases import WriteAliasResolver
from souq_indexer.pipeline import BatchProcessor, ShutdownRequestedError
from souq_indexer.routing import TOPIC_DLQ, TOPIC_RETRY_5M, TOPIC_RETRY_30S


class Harness:
    def __init__(self, aliases: dict[str, list[str]] | None = None, initial: int = 1000) -> None:
        self.os = FakeOpenSearch(aliases)
        self.aimd = AimdController(initial=initial)
        self.idles: list[float] = []
        self.stop = False
        self.processor = BatchProcessor(
            self.os,
            WriteAliasResolver(self.os, "products_write", refresh_s=0),
            FakeEmbedder(),
            self.aimd,
            in_process_attempts=3,
            idle=self.idles.append,
            should_stop=lambda: self.stop,
            now_ms=lambda: NOW_MS,
        )


def test_clean_batch_indexes_every_event_and_grows_the_batch() -> None:
    h = Harness()
    result = h.processor.process([make_env(f"S{i}", 1, offset=i) for i in range(5)])
    assert result.indexed == 5
    assert result.routed == []
    assert len(h.os.calls) == 1
    assert h.aimd.batch_size == 1100  # additive increase after a clean batch
    version, doc = h.os.live_doc("products_v1", "S0") or (0, None)
    assert version == 1
    assert doc is not None
    assert "unmapped_extra" not in doc
    assert len(doc["embedding"]) == 1024
    assert doc["colour"] == "black"
    # freshness = now - outbox_created_at (5 s in the fixture)
    assert result.freshness_s == [pytest.approx(5.0)] * 5


def test_external_version_conflict_is_success_and_never_regresses() -> None:
    h = Harness()
    h.processor.process([make_env("S1", 7)])
    result = h.processor.process([make_env("S1", 5), make_env("S1", 7, offset=1)])
    assert result.indexed == 2
    assert result.routed == []
    assert h.os.live_doc("products_v1", "S1")[0] == 7  # type: ignore[index]


def test_partial_failure_retries_only_the_failed_items() -> None:
    h = Harness()
    h.os.script.append(fail_items(lambda op, m: m["_id"] == "S2", 500, "internal_server_error"))
    result = h.processor.process([make_env(f"S{i}", 1, offset=i) for i in range(4)])
    assert result.indexed == 4
    assert [len(c) for c in h.os.calls] == [4, 1]
    assert h.os.calls[1][0][1]["_id"] == "S2"
    assert h.idles == [0.5]  # one backoff between attempts


def test_mapping_error_goes_straight_to_dlq_without_retry() -> None:
    h = Harness()
    h.os.script.append(
        fail_items(lambda op, m: m["_id"] == "BAD", 400, "mapper_parsing_exception", "bad field")
    )
    result = h.processor.process([make_env("OK", 1), make_env("BAD", 1, offset=1)])
    assert result.indexed == 1
    assert len(h.os.calls) == 1
    [routed] = result.routed
    assert routed.route.topic == TOPIC_DLQ
    assert routed.error_class == "mapper_parsing_exception"
    assert "bad field" in routed.error_message


def test_transient_failures_move_down_the_retry_ladder() -> None:
    for attempt, expected_topic, delay in ((0, TOPIC_RETRY_30S, 30_000), (1, TOPIC_RETRY_5M, 300_000)):
        h = Harness()
        for _ in range(3):
            h.os.script.append(fail_items(lambda op, m: True, 500, "internal_server_error"))
        topic = "catalog.changes" if attempt == 0 else TOPIC_RETRY_30S
        result = h.processor.process([make_env("S1", 1, attempt=attempt, topic=topic)])
        assert len(h.os.calls) == 3  # in-process attempts
        [routed] = result.routed
        assert routed.route.topic == expected_topic
        assert routed.route.attempt == attempt + 1
        assert routed.route.not_before_ms == NOW_MS + delay
        assert routed.original_topic == "catalog.changes"

    h = Harness()
    for _ in range(3):
        h.os.script.append(fail_items(lambda op, m: True, 500, "internal_server_error"))
    result = h.processor.process([make_env("S1", 1, attempt=2, topic=TOPIC_RETRY_5M)])
    assert result.routed[0].route.topic == TOPIC_DLQ


def test_rejections_halve_batch_back_off_and_do_not_spend_attempts() -> None:
    h = Harness(initial=1000)
    for _ in range(5):  # more rejections than in-process attempts
        h.os.script.append(fail_items(lambda op, m: True, 429, "es_rejected_execution_exception"))
    result = h.processor.process([make_env(f"S{i}", 1, offset=i) for i in range(3)])
    assert result.indexed == 3
    assert result.routed == []
    assert result.rejections == 15
    assert h.aimd.batch_size == 50  # 1000 -> 500 -> 250 -> 125 -> 62 -> 50 (floor), no growth this batch
    assert h.idles == [0.5, 1.0, 2.0, 4.0, 8.0]  # exponential backoff while paused


def test_shrunken_batch_splits_the_retry_into_smaller_bulk_requests() -> None:
    h = Harness(initial=100)
    h.aimd.min_size = 2
    h.os.script.append(TransportError(429, "es_rejected_execution_exception", {}))
    h.aimd._size = 4
    result = h.processor.process([make_env(f"S{i}", 1, offset=i) for i in range(4)])
    assert result.indexed == 4
    assert [len(c) for c in h.os.calls] == [4, 2, 2]  # 4 -> rejected -> batch 2


def test_cluster_outage_is_backpressure_not_dlq() -> None:
    h = Harness()
    for _ in range(8):
        h.os.script.append(OSConnectionError("N/A", "connection refused", None))
    result = h.processor.process([make_env("S1", 1)])
    assert result.indexed == 1
    assert result.routed == []
    assert len(h.os.calls) == 9


def test_shutdown_during_backoff_abandons_the_batch() -> None:
    h = Harness()
    h.os.script.append(OSConnectionError("N/A", "connection refused", None))

    def idle(_: float) -> None:
        h.stop = True

    h.processor._idle = idle
    with pytest.raises(ShutdownRequestedError):
        h.processor.process([make_env("S1", 1)])


def test_dual_write_bulks_to_every_index_in_the_write_alias() -> None:
    h = Harness(aliases={"products_write": ["products_v1", "products_v2"]})
    result = h.processor.process([make_env("S1", 3)])
    assert result.indexed == 1
    assert {m["_index"] for _, m, _ in h.os.calls[0]} == {"products_v1", "products_v2"}
    assert h.os.live_doc("products_v1", "S1")[0] == 3  # type: ignore[index]
    assert h.os.live_doc("products_v2", "S1")[0] == 3  # type: ignore[index]


def test_dual_write_failure_on_one_index_routes_the_event() -> None:
    h = Harness(aliases={"products_write": ["products_v1", "products_v2"]})
    h.os.script.append(
        fail_items(lambda op, m: m["_index"] == "products_v2", 400, "strict_dynamic_mapping_exception")
    )
    result = h.processor.process([make_env("S1", 3)])
    assert result.indexed == 0
    assert result.routed[0].route.topic == TOPIC_DLQ
    assert "[products_v2]" in result.routed[0].error_message


def test_deletes_use_external_versions_too() -> None:
    h = Harness()
    h.processor.process([make_env("S1", 2)])
    result = h.processor.process([make_env("S1", 3, event_type="product.deleted", offset=1)])
    assert result.indexed == 1
    op, meta, src = h.os.calls[-1][0]
    assert (op, meta["version"], meta["version_type"], src) == ("delete", 3, "external", None)
    # a stale upsert arriving after the delete is a 409, not a resurrection
    h.processor.process([make_env("S1", 2, offset=2)])
    assert h.os.live_doc("products_v1", "S1") == (3, None)


def test_unparseable_message_is_dead_lettered() -> None:
    h = Harness()
    bad = make_env("S1", 1, value=b"{not json")
    result = h.processor.process([bad, make_env("S2", 1, offset=1)])
    assert result.indexed == 1
    [routed] = result.routed
    assert routed.route.topic == TOPIC_DLQ
    assert routed.error_class == "invalid_json"


def test_missing_write_alias_waits_instead_of_guessing() -> None:
    h = Harness(aliases={})

    def idle(_: float) -> None:
        h.os.indices.aliases["products_write"] = ["products_v1"]

    h.processor._idle = idle
    result = h.processor.process([make_env("S1", 1)])
    assert result.indexed == 1
    assert h.os.calls[0][0][1]["_index"] == "products_v1"


def test_byte_cap_splits_bulk_requests() -> None:
    h = Harness()
    h.processor._max_bulk_bytes = 6_000  # just over one doc (incl. a 1024-d embedding) per request
    result = h.processor.process([make_env(f"S{i}", 1, offset=i) for i in range(3)])
    assert result.indexed == 3
    assert len(h.os.calls) == 3

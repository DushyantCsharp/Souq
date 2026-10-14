"""IndexerService: commit-after-index, retry-topic not_before pausing, rewind on failure, routing headers."""

from __future__ import annotations

import time

from indexer_fakes import FakeConsumer, FakeMessage, FakeOpenSearch, FakeProducer, fail_items, make_value

from souq_common.embeddings import FakeEmbedder
from souq_indexer.aimd import AimdController
from souq_indexer.aliases import WriteAliasResolver
from souq_indexer.events import encode_headers, header_map
from souq_indexer.pipeline import BatchProcessor
from souq_indexer.service import IndexerService

CHANGES = "catalog.changes"
RETRY_30S = "catalog.retry.30s"


def msg(topic: str, partition: int, offset: int, sku: str, ver: int, **headers: str | int) -> FakeMessage:
    hdrs = encode_headers(
        [("traceparent", "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"), ("sku", sku)]
        + [(k, v) for k, v in headers.items()]
    )
    return FakeMessage(topic, partition, offset, sku.encode(), make_value(sku, ver), hdrs)


def build(
    partitions: dict[tuple[str, int], list[FakeMessage]], os_: FakeOpenSearch | None = None
) -> tuple[IndexerService, FakeConsumer, FakeProducer, FakeOpenSearch]:
    consumer = FakeConsumer(partitions)
    producer = FakeProducer()
    client = os_ or FakeOpenSearch()
    aimd = AimdController()
    holder: dict[str, IndexerService] = {}
    processor = BatchProcessor(
        client,
        WriteAliasResolver(client, "products_write", refresh_s=0),
        FakeEmbedder(),
        aimd,
        idle=lambda s: holder["svc"].idle(0),
        should_stop=lambda: holder["svc"].stopping,
    )
    svc = IndexerService(consumer, producer, processor, aimd, batch_linger_ms=1, lag_report_interval_s=3600)
    holder["svc"] = svc
    svc._consumer.subscribe([], on_assign=svc._on_assign)
    consumer.assign_all()
    return svc, consumer, producer, client


def test_commits_next_offset_per_partition_only_after_indexing() -> None:
    parts = {
        (CHANGES, 0): [msg(CHANGES, 0, i, f"A{i}", 1) for i in range(3)],
        (CHANGES, 1): [msg(CHANGES, 1, 0, "B0", 1)],
    }
    svc, consumer, _, client = build(parts)
    svc.run_once()
    assert consumer.commit_map == {(CHANGES, 0): 3, (CHANGES, 1): 1}
    assert len(client.docs) == 4
    assert svc.stats.indexed == 4


def test_not_yet_due_retry_message_pauses_and_seeks_back() -> None:
    future = int(time.time() * 1000) + 60_000
    due = int(time.time() * 1000) - 1
    parts = {
        (RETRY_30S, 0): [
            msg(RETRY_30S, 0, 0, "R0", 1, attempt=1, not_before=due),
            msg(RETRY_30S, 0, 1, "R1", 1, attempt=1, not_before=future),
            msg(RETRY_30S, 0, 2, "R2", 1, attempt=1, not_before=future),
        ]
    }
    svc, consumer, _, client = build(parts)
    svc.run_once()
    # R0 indexed and committed; R1 not due: partition paused and rewound to it; R2 untouched
    assert set(client.docs) == {("products_v1", "R0")}
    assert consumer.commit_map == {(RETRY_30S, 0): 1}
    assert (RETRY_30S, 0) in consumer.paused
    assert consumer.positions[(RETRY_30S, 0)] == 1
    # still not due: nothing more happens
    svc.run_once()
    assert set(client.docs) == {("products_v1", "R0")}
    # once due, the partition resumes from R1
    svc._retry_paused[(RETRY_30S, 0)] = 0
    for m in parts[(RETRY_30S, 0)][1:]:
        m._headers = encode_headers([("sku", m.key().decode()), ("attempt", 1), ("not_before", due)])  # type: ignore[union-attr]
    svc.run_once()
    assert set(client.docs) == {("products_v1", f"R{i}") for i in range(3)}
    assert consumer.commit_map == {(RETRY_30S, 0): 3}


def test_routed_messages_are_acknowledged_before_commit_and_carry_routing_headers() -> None:
    client = FakeOpenSearch()
    client.script.append(fail_items(lambda op, m: True, 400, "mapper_parsing_exception", "bad"))
    parts = {(CHANGES, 0): [msg(CHANGES, 0, 0, "X", 4, event_id="9", version=4, attempt=0)]}
    svc, consumer, producer, _ = build(parts, client)
    svc.run_once()
    [out] = producer.produced
    assert out["topic"] == "catalog.dlq"
    assert out["key"] == b"X"
    headers = header_map(out["headers"])
    assert headers["error_class"] == "mapper_parsing_exception"
    assert headers["original_topic"] == CHANGES
    assert headers["attempt"] == "1"
    assert headers["traceparent"].startswith("00-0af7651916cd43dd")  # original trace context kept
    assert headers["event_id"] == "9"
    assert "not_before" not in headers
    assert consumer.commit_map == {(CHANGES, 0): 1}


def test_failed_routing_delivery_rewinds_and_does_not_commit() -> None:
    client = FakeOpenSearch()
    client.script.append(fail_items(lambda op, m: True, 400, "mapper_parsing_exception"))
    parts = {(CHANGES, 0): [msg(CHANGES, 0, 0, "X", 1), msg(CHANGES, 0, 1, "Y", 1)]}
    svc, consumer, producer, _ = build(parts, client)
    producer.fail = True
    svc.run_once()
    assert consumer.commit_map == {}
    assert consumer.positions[(CHANGES, 0)] == 0  # rewound: both messages will be re-consumed
    producer.fail = False
    svc.run_once()
    assert consumer.commit_map == {(CHANGES, 0): 2}


def test_retry_topic_routing_keeps_original_topic_and_sets_not_before() -> None:
    client = FakeOpenSearch()
    for _ in range(3):
        client.script.append(fail_items(lambda op, m: True, 500, "internal_server_error"))
    parts = {(CHANGES, 3): [msg(CHANGES, 3, 7, "T", 1)]}
    svc, consumer, producer, _ = build(parts, client)
    before = int(time.time() * 1000)
    svc.run_once()
    [out] = producer.produced
    assert out["topic"] == RETRY_30S
    headers = header_map(out["headers"])
    assert headers["attempt"] == "1"
    assert int(headers["not_before"]) >= before + 30_000
    assert consumer.commit_map == {(CHANGES, 3): 8}


def test_stop_request_closes_without_committing_unprocessed_messages() -> None:
    parts = {(CHANGES, 0): [msg(CHANGES, 0, 0, "A", 1)]}
    svc, consumer, _, _ = build(parts)
    svc.request_stop()
    svc.run()
    assert consumer.closed
    assert consumer.commit_map == {}


def test_lag_and_dlq_depth_reporting() -> None:
    parts = {(CHANGES, 0): [msg(CHANGES, 0, i, f"A{i}", 1) for i in range(5)]}
    svc, _, _, _ = build(parts)
    assert svc.report_lag()[f"{CHANGES}/0"] == 5
    svc.run_once()
    assert svc.report_lag()[f"{CHANGES}/0"] == 0
    assert svc.report_dlq_depth() == 0

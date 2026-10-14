"""Indexer against real Redpanda + OpenSearch (KAFKA_BOOTSTRAP / OPENSEARCH_URL; skipped if unreachable).

Uses throwaway topics and a throwaway index + write alias, so it never touches the shared stack's data.
The service's topic constants are pointed at the scratch topics for the duration of the test.
"""

from __future__ import annotations

import io
import json
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from confluent_kafka import Consumer, KafkaException, Producer, TopicPartition
from confluent_kafka.admin import AdminClient, NewTopic  # type: ignore[attr-defined]
from indexer_fakes import make_value
from opensearchpy import OpenSearch

import souq_indexer.dlq_cli as dlq_mod
import souq_indexer.routing as routing_mod
import souq_indexer.service as service_mod
from souq_common.embeddings import FakeEmbedder
from souq_common.settings import get_settings
from souq_indexer.aimd import AimdController
from souq_indexer.aliases import WriteAliasResolver
from souq_indexer.events import encode_headers
from souq_indexer.kafka_io import make_consumer, make_producer
from souq_indexer.pipeline import BatchProcessor
from souq_indexer.service import IndexerService

pytestmark = pytest.mark.integration

TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"

TEMPLATE = Path(__file__).resolve().parents[3] / "infra" / "opensearch" / "templates" / "products_v1.json"


class Env:
    def __init__(self) -> None:
        s = get_settings()
        self.bootstrap = s.kafka_bootstrap
        self.os = OpenSearch(hosts=[s.opensearch_url], timeout=30)
        tag = uuid.uuid4().hex[:8]
        self.changes = f"it.{tag}.changes"
        self.retry = f"it.{tag}.retry30s"
        self.dlq = f"it.{tag}.dlq"
        self.index = f"it_idx_{tag}"
        self.alias = f"{self.index}_write"
        self.group = f"it-indexer-{tag}"

    def produce(
        self, topic: str, sku: str, version: int, value: bytes | None = None, **headers: str | int
    ) -> None:
        p = Producer({"bootstrap.servers": self.bootstrap})
        hdrs = encode_headers([("sku", sku), ("version", version), *headers.items()])
        p.produce(topic, key=sku.encode(), value=value or make_value(sku, version), headers=hdrs)
        assert p.flush(10) == 0

    def doc_version(self, sku: str) -> int | None:
        resp = self.os.get(index=self.index, id=sku, ignore=404, _source=False)
        return int(resp["_version"]) if resp.get("found") else None

    def fully_committed(self) -> bool:
        """Every non-empty partition's committed offset is its end offset."""
        ends = self.end_offsets()
        committed = self.committed()
        return all(committed.get(tp) == end for tp, end in ends.items() if end > 0)

    def committed(self) -> dict[tuple[str, int], int]:
        c = Consumer({"bootstrap.servers": self.bootstrap, "group.id": self.group})
        try:
            parts = [TopicPartition(t, p) for t in (self.changes, self.retry) for p in range(3)]
            return {(p.topic, p.partition): p.offset for p in c.committed(parts, timeout=10)}
        finally:
            c.close()

    def end_offsets(self) -> dict[tuple[str, int], int]:
        c = Consumer({"bootstrap.servers": self.bootstrap, "group.id": "it-probe"})
        try:
            return {
                (t, p): c.get_watermark_offsets(TopicPartition(t, p), timeout=10)[1]
                for t in (self.changes, self.retry)
                for p in range(3)
            }
        finally:
            c.close()


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    e = Env()
    admin = AdminClient({"bootstrap.servers": e.bootstrap})
    try:
        futures = admin.create_topics(
            [NewTopic(t, num_partitions=3, replication_factor=1) for t in (e.changes, e.retry)]
        )
        for f in futures.values():
            f.result(10)
        e.os.info()
    except (KafkaException, Exception) as exc:
        pytest.skip(f"kafka/opensearch not reachable: {exc}")
    template = json.loads(TEMPLATE.read_text())["template"]
    e.os.indices.create(
        index=e.index,
        body={"settings": template["settings"], "mappings": template["mappings"], "aliases": {e.alias: {}}},
    )
    monkeypatch.setattr(service_mod, "TOPIC_CHANGES", e.changes)
    monkeypatch.setattr(service_mod, "RETRY_TOPICS", (e.retry,))
    monkeypatch.setattr(service_mod, "CONSUMED_TOPICS", (e.changes, e.retry))
    monkeypatch.setattr(service_mod, "_DEFAULT_RETRY_ATTEMPT", {e.retry: 1})
    monkeypatch.setattr(service_mod, "TOPIC_DLQ", e.dlq)
    monkeypatch.setattr(routing_mod, "TOPIC_DLQ", e.dlq)
    monkeypatch.setattr(dlq_mod, "TOPIC_DLQ", e.dlq)
    monkeypatch.setattr(dlq_mod, "TOPIC_CHANGES", e.changes)
    try:
        yield e
    finally:
        admin.delete_topics([e.changes, e.retry, e.dlq])
        e.os.indices.delete(index=e.index, ignore=404)


def build(e: Env) -> IndexerService:
    client = OpenSearch(hosts=[get_settings().opensearch_url], timeout=30, max_retries=0)
    aimd = AimdController()
    holder: dict[str, IndexerService] = {}
    processor = BatchProcessor(
        client,
        WriteAliasResolver(client, e.alias, refresh_s=10),
        FakeEmbedder(),
        aimd,
        idle=lambda s: holder["svc"].idle(s),
        should_stop=lambda: holder["svc"].stopping,
    )
    svc = IndexerService(
        make_consumer(e.bootstrap, e.group, "it-indexer"),
        make_producer(e.bootstrap, "it-indexer"),
        processor,
        aimd,
        batch_linger_ms=200,
        lag_report_interval_s=1,
    )
    holder["svc"] = svc
    return svc


def run_until(svc: IndexerService, predicate: Any, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


def test_indexes_commits_and_honours_not_before(env: Env) -> None:
    for i in range(30):
        env.produce(env.changes, f"SKU{i:03d}", 1)
    for i in range(5):
        env.produce(env.changes, f"SKU{i:03d}", 2)
    env.produce(env.changes, "SKU000", 1)  # stale duplicate after v2: must not regress
    now_ms = int(time.time() * 1000)
    env.produce(env.retry, "DUE", 3, attempt=1, not_before=now_ms - 1, original_topic=env.changes)
    produced_at = time.monotonic()
    env.produce(env.retry, "LATER", 3, attempt=1, not_before=now_ms + 6000, original_topic=env.changes)

    svc = build(env)
    t = threading.Thread(target=svc.run, daemon=True)
    t.start()
    try:
        assert run_until(svc, lambda: env.doc_version("SKU029") == 1 and env.doc_version("DUE") == 3, 30)
        # the retry message that is not yet due is held back (partition paused, not slept on) ...
        assert env.doc_version("LATER") is None
        assert time.monotonic() - produced_at < 6
        # ... while catalog.changes keeps flowing
        env.produce(env.changes, "SKU100", 1)
        assert run_until(svc, lambda: env.doc_version("SKU100") == 1, 5)
        assert env.doc_version("LATER") is None
        # and is indexed once due
        assert run_until(svc, lambda: env.doc_version("LATER") == 3, 20)
        assert time.monotonic() - produced_at >= 5.5
        assert [env.doc_version(f"SKU{i:03d}") for i in range(5)] == [2] * 5
        assert run_until(svc, env.fully_committed, 15), (env.committed(), env.end_offsets())
        assert svc.stats.routed == {}
        assert run_until(svc, lambda: bool(svc.stats.lag) and not any(svc.stats.lag.values()), 10)
    finally:
        svc.request_stop()
        t.join(timeout=60)
    assert not t.is_alive()


def test_poison_message_is_dead_lettered_then_listed_and_redriven(env: Env) -> None:
    poison = json.loads(make_value("POISON", 1))
    poison["product"]["price"] = "not-a-number"  # scaled_float: mapper/document parsing exception
    env.produce(env.changes, "POISON", 1, value=json.dumps(poison).encode(), traceparent=TRACEPARENT)
    env.produce(env.changes, "FINE", 1)
    svc = build(env)
    t = threading.Thread(target=svc.run, daemon=True)
    t.start()
    try:
        assert run_until(svc, lambda: env.doc_version("FINE") == 1 and svc.stats.routed.get(env.dlq) == 1, 30)
        assert env.doc_version("POISON") is None
        assert run_until(svc, env.fully_committed, 15)
    finally:
        svc.request_stop()
        t.join(timeout=60)

    reader = dlq_mod.DlqReader(env.bootstrap)
    try:
        [record] = list(reader)
    finally:
        reader.close()
    meta = record.meta
    assert record.sku == "POISON"
    assert meta["attempt"] == "1"
    assert meta["original_topic"] == env.changes
    assert meta["traceparent"] == TRACEPARENT
    assert "parsing" in meta["error_class"] or "mapper" in meta["error_class"], meta["error_class"]

    out = io.StringIO()
    args = dlq_mod.build_parser().parse_args(["redrive", "--sku-from", "P", "--sku-to", "Q"])
    reader = dlq_mod.DlqReader(env.bootstrap)
    try:
        assert dlq_mod.cmd_redrive(iter(reader), args, out, make_producer(env.bootstrap, "it-dlq")) == 0
    finally:
        reader.close()
    assert "1 message(s) redriven" in out.getvalue()
    c = Consumer({"bootstrap.servers": env.bootstrap, "group.id": f"it-{uuid.uuid4().hex[:6]}"})
    c.assign([TopicPartition(env.changes, p, 0) for p in range(3)])
    redriven = []
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not redriven:
        msg = c.poll(1.0)
        if msg is not None and msg.error() is None and msg.key() == b"POISON":
            hdrs = dict(msg.headers() or [])
            if hdrs.get("attempt") == b"0" and "error_class" not in hdrs:
                redriven.append(msg)
    c.close()
    assert redriven, "redriven message not found on the changes topic"

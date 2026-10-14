"""Kafka (Redpanda) and OpenSearch helpers for black-box assertions on the pipeline."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from souq_testing.containers import TOPICS, opensearch_request
from souq_testing.factories import event_headers


@dataclass(frozen=True)
class Delivered:
    topic: str
    partition: int
    offset: int


def produce_events(
    bootstrap: str,
    events: list[Mapping[str, Any]],
    topic: str = "catalog.changes",
    partition: int | None = None,
) -> list[Delivered]:
    """Produce contract-shaped events (key = sku) with the producer settings from CLAUDE.md."""
    from confluent_kafka import KafkaException, Producer

    producer = Producer(
        {
            "bootstrap.servers": bootstrap,
            "enable.idempotence": True,
            "acks": "all",
            "max.in.flight.requests.per.connection": 5,
        }
    )
    delivered: list[Delivered] = []
    errors: list[str] = []

    def on_delivery(err: Any, msg: Any) -> None:
        if err is not None:
            errors.append(str(err))
        else:
            delivered.append(Delivered(msg.topic(), msg.partition(), msg.offset()))

    for event in events:
        kwargs: dict[str, Any] = {
            "key": str(event["sku"]).encode(),
            "value": json.dumps(event, ensure_ascii=False).encode(),
            "headers": event_headers(event),
            "on_delivery": on_delivery,
        }
        if partition is not None:
            kwargs["partition"] = partition
        producer.produce(topic, **kwargs)
        # Flush per event so the returned offsets are in send order.
        producer.flush(15)
    if errors:
        raise KafkaException(errors[0])
    return delivered


def ensure_topics(bootstrap: str) -> None:
    """Create the contract topics if missing (idempotent; mirrors infra/redpanda/topics.sh)."""
    from confluent_kafka import KafkaError
    from confluent_kafka.admin import AdminClient, NewTopic

    admin = AdminClient({"bootstrap.servers": bootstrap})
    existing = set(admin.list_topics(timeout=10).topics)
    missing = [
        NewTopic(t, num_partitions=p, replication_factor=1) for t, p in TOPICS.items() if t not in existing
    ]
    for topic, future in admin.create_topics(missing).items() if missing else ():
        try:
            future.result(15)
        except Exception as exc:
            err = exc.args[0] if exc.args else None
            if not (isinstance(err, KafkaError) and err.code() == KafkaError.TOPIC_ALREADY_EXISTS):
                raise RuntimeError(f"creating {topic} failed: {exc}") from exc


def read_topic(bootstrap: str, topic: str, timeout_s: float = 5.0) -> list[dict[str, Any]]:
    """Read every record currently in ``topic`` (fresh group, from the beginning)."""
    from confluent_kafka import Consumer, TopicPartition

    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap,
            "group.id": f"souq-it-reader-{uuid.uuid4().hex[:8]}",
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
        }
    )
    try:
        md = consumer.list_topics(topic, timeout=10)
        parts = [TopicPartition(topic, p) for p in md.topics[topic].partitions]
        ends = {}
        for tp in parts:
            _low, high = consumer.get_watermark_offsets(tp, timeout=10)
            ends[tp.partition] = high
        consumer.assign([TopicPartition(topic, p, 0) for p in ends])
        remaining = {p for p, high in ends.items() if high > 0}
        records: list[dict[str, Any]] = []
        deadline = time.monotonic() + timeout_s
        while remaining and time.monotonic() < deadline:
            msg = consumer.poll(0.5)
            if msg is None or msg.error():
                continue
            headers = {k: (v.decode() if isinstance(v, bytes) else v) for k, v in (msg.headers() or [])}
            records.append(
                {
                    "key": msg.key().decode() if msg.key() else None,
                    "value": json.loads(msg.value()) if msg.value() else None,
                    "headers": headers,
                    "partition": msg.partition(),
                    "offset": msg.offset(),
                }
            )
            if msg.offset() + 1 >= ends[msg.partition()]:
                remaining.discard(msg.partition())
        return records
    finally:
        consumer.close()


# --------------------------------------------------------------------------- OpenSearch


def os_get_doc(base: str, index: str, doc_id: str) -> dict[str, Any] | None:
    import urllib.error

    try:
        return opensearch_request(base, "GET", f"/{index}/_doc/{doc_id}")  # type: ignore[no-any-return]
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def os_refresh(base: str, index: str = "_all") -> None:
    opensearch_request(base, "POST", f"/{index}/_refresh")


def os_alias_indices(base: str, alias: str) -> list[str]:
    import urllib.error

    try:
        resp = opensearch_request(base, "GET", f"/_alias/{alias}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []
        raise
    return sorted(resp)


def os_count(base: str, index: str, query: Mapping[str, Any] | None = None) -> int:
    body = {"query": dict(query)} if query else None
    return int(opensearch_request(base, "POST" if body else "GET", f"/{index}/_count", body)["count"])


def wait_for(predicate: Callable[[], Any], timeout_s: float, what: str, interval_s: float = 0.5) -> Any:
    """Poll until ``predicate()`` is truthy; return its value. Fails with ``what`` in the message."""
    deadline = time.monotonic() + timeout_s
    last: Any = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval_s)
    raise AssertionError(f"timed out after {timeout_s:.0f}s waiting for {what} (last={last!r})")


def wait_for_doc(
    base: str, index: str, sku: str, timeout_s: float = 60, min_version: int | None = None
) -> dict[str, Any]:
    def check() -> dict[str, Any] | None:
        doc = os_get_doc(base, index, sku)
        if not doc or not doc.get("found"):
            return None
        if min_version is not None and int(doc.get("_version", 0)) < min_version:
            return None
        return doc

    return wait_for(check, timeout_s, f"{index}/{sku} (version>={min_version})")  # type: ignore[no-any-return]

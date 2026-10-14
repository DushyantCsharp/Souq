"""Relay against a real Postgres + Redpanda (DATABASE_URL / KAFKA_BOOTSTRAP; skipped if unreachable).

Runs in a throwaway database (real packages/db outbox migration) and a throwaway topic, so it never
publishes or marks rows that belong to the shared stack.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from confluent_kafka import Consumer, KafkaException, TopicPartition
from relay_it_env import scratch_database, scratch_topic

from souq_common.settings import get_settings
from souq_outbox_relay.kafka import make_producer
from souq_outbox_relay.relay import OutboxRelay

pytestmark = pytest.mark.integration


class Env:
    def __init__(self, dsn: str, topic: str) -> None:
        self.dsn = dsn
        self.topic = topic
        self.bootstrap = get_settings().kafka_bootstrap

    def connect(self) -> psycopg.Connection[Any]:
        return psycopg.connect(self.dsn, autocommit=True, connect_timeout=3)

    def relay(self) -> OutboxRelay:
        return OutboxRelay(
            self.connect, make_producer(self.bootstrap, "relay-it"), topic=self.topic, batch_size=500
        )

    def insert(self, n: int, versions_per_sku: int = 1) -> None:
        with self.connect() as conn, conn.cursor() as cur:
            for v in range(1, versions_per_sku + 1):
                for i in range(n):
                    sku = f"SKU{i:05d}"
                    cur.execute(
                        "INSERT INTO outbox (aggregate_id, event_type, payload, trace_context)"
                        " VALUES (%s, 'product.upserted', %s, %s)",
                        (sku, json.dumps({"sku": sku, "version": v}), json.dumps({})),
                    )

    def read_all(self, expected: int) -> list[Any]:
        c = Consumer({"bootstrap.servers": self.bootstrap, "group.id": f"it-{uuid.uuid4().hex[:6]}"})
        md = c.list_topics(self.topic, timeout=5)
        c.assign([TopicPartition(self.topic, p, 0) for p in md.topics[self.topic].partitions])
        found: list[Any] = []
        idle = 0
        while len(found) < expected and idle < 10:
            msg = c.poll(1.0)
            if msg is None:
                idle += 1
            elif msg.error() is None:
                found.append(msg)
        c.close()
        return found


@pytest.fixture
def env() -> Iterator[Env]:
    settings = get_settings()
    try:
        with psycopg.connect(settings.database_url, connect_timeout=3):
            pass
    except psycopg.Error as exc:
        pytest.skip(f"postgres not reachable: {exc}")
    try:
        with (
            scratch_database(settings.database_url, only_migration="_outbox") as dsn,
            scratch_topic(settings.kafka_bootstrap) as topic,
        ):
            yield Env(dsn, topic)
    except KafkaException as exc:
        pytest.skip(f"kafka not reachable: {exc}")


def _drain(relay: OutboxRelay) -> None:
    while relay.publish_batch() > 0:
        pass


def test_publishes_marks_and_preserves_per_sku_order(env: Env) -> None:
    env.insert(20, versions_per_sku=3)
    _drain(env.relay())
    with env.connect() as conn:
        assert conn.execute("SELECT count(*) FROM outbox WHERE published_at IS NULL").fetchone() == (0,)
    msgs = env.read_all(60)
    assert len(msgs) == 60
    by_sku: dict[str, list[int]] = {}
    for m in msgs:
        by_sku.setdefault(m.key().decode(), []).append(json.loads(m.value())["version"])
        headers = dict(m.headers())
        assert headers["attempt"] == b"0"
        assert headers["sku"] == m.key()
    # a single relay publishes in id order, and key=sku keeps each SKU on one partition
    assert all(v == [1, 2, 3] for v in by_sku.values())


def test_two_replicas_never_publish_a_row_twice(env: Env) -> None:
    env.insert(1500)
    relays = [env.relay(), env.relay()]
    threads = [threading.Thread(target=_drain, args=(r,)) for r in relays]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    msgs = env.read_all(1500)
    event_ids = [json.loads(m.value())["event_id"] for m in msgs]
    assert len(event_ids) == 1500
    assert len(set(event_ids)) == 1500  # SKIP LOCKED: disjoint batches
    assert sum(r.stats.published for r in relays) == 1500

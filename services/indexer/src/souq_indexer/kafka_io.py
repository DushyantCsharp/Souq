"""confluent-kafka client construction. Producer settings and acknowledged produce are shared with the
outbox relay via ``souq_common.kafka``."""

from __future__ import annotations

from typing import Any

from confluent_kafka import Consumer

from souq_common.kafka import DeliveryError, OutgoingMessage, make_producer, produce_and_wait, producer_config

__all__ = [
    "DeliveryError",
    "OutgoingMessage",
    "consumer_config",
    "make_consumer",
    "make_producer",
    "produce_and_wait",
    "producer_config",
]


def consumer_config(bootstrap: str, group_id: str, client_id: str) -> dict[str, Any]:
    return {
        "bootstrap.servers": bootstrap,
        "group.id": group_id,
        "client.id": client_id,
        # Offsets are committed explicitly, and only after the batch is indexed or routed.
        "enable.auto.commit": False,
        "enable.auto.offset.store": False,
        "auto.offset.reset": "earliest",
        "partition.assignment.strategy": "cooperative-sticky",
        "session.timeout.ms": 45_000,
        "max.poll.interval.ms": 300_000,
        "fetch.wait.max.ms": 100,
    }


def make_consumer(bootstrap: str, group_id: str, client_id: str) -> Consumer:
    return Consumer(consumer_config(bootstrap, group_id, client_id))

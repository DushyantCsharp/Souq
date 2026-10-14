"""Kafka producer configuration and acknowledged produce, shared by outbox-relay and the indexer.

One place for the CLAUDE.md producer contract (``enable.idempotence``, ``acks=all``,
``max.in.flight.requests.per.connection=5``) and its tuning, so relay and indexer cannot drift apart.
Owner: workers agent.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from confluent_kafka import KafkaError, Message, Producer


def producer_config(bootstrap: str, client_id: str) -> dict[str, Any]:
    # Idempotence makes librdkafka's internal retries duplicate-free and keeps per-partition order with up
    # to 5 in-flight requests; acks=all means an acknowledged message survives a broker failover.
    return {
        "bootstrap.servers": bootstrap,
        "client.id": client_id,
        "enable.idempotence": True,
        "acks": "all",
        "max.in.flight.requests.per.connection": 5,
        # A few ms of batching: relay batches are 500 rows, so this mostly coalesces per partition.
        "linger.ms": 5,
        "batch.num.messages": 1000,
        "compression.type": "lz4",
        # Upper bound on one delivery including retries; callers block on flush() with a similar timeout.
        "message.timeout.ms": 30_000,
    }


def make_producer(bootstrap: str, client_id: str) -> Producer:
    return Producer(producer_config(bootstrap, client_id))


@dataclass(frozen=True, slots=True)
class OutgoingMessage:
    topic: str
    key: bytes | None
    value: bytes | None
    headers: list[tuple[str, bytes]]


class DeliveryError(RuntimeError):
    pass


def produce_and_wait(producer: Any, messages: Sequence[OutgoingMessage], timeout_s: float = 30.0) -> None:
    """Produce every message and block until each is acknowledged (acks=all); raise otherwise.

    Callers only record progress (mark outbox rows published, commit consumer offsets) after this returns,
    which is what makes the pipeline at-least-once.
    """
    if not messages:
        return
    errors: list[str] = []
    acked = 0

    def on_delivery(err: KafkaError | None, msg: Message) -> None:
        nonlocal acked
        if err is not None:
            errors.append(f"{msg.topic()}: {err.str()}")
        else:
            acked += 1

    for m in messages:
        while True:
            try:
                producer.produce(
                    m.topic, key=m.key, value=m.value, headers=m.headers, on_delivery=on_delivery
                )
                break
            except BufferError:
                producer.poll(0.5)  # local queue full: serve delivery callbacks, then retry this message
    remaining = producer.flush(timeout_s)
    if errors:
        raise DeliveryError(f"{len(errors)} deliveries failed, first: {errors[0]}")
    if remaining or acked != len(messages):
        raise DeliveryError(f"{len(messages) - acked} of {len(messages)} deliveries not acknowledged")

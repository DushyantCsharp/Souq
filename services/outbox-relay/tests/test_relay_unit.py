"""Outbox relay: message shape, trace propagation, publish-then-mark, rollback on delivery failure."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from souq_outbox_relay.kafka import DeliveryError, OutgoingMessage, produce_and_wait, producer_config
from souq_outbox_relay.relay import MARK_PUBLISHED, OutboxRelay, build_message, iso_ms

TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"


def row(i: int, sku: str = "B000123", version: int | None = 7, trace: Any = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"sku": sku, "title_en": "x"}
    if version is not None:
        payload["version"] = version
    return {
        "id": i,
        "aggregate_id": sku,
        "event_type": "product.upserted",
        "payload": payload,
        "trace_context": trace if trace is not None else {"traceparent": TRACEPARENT},
        "created_at": datetime(2026, 10, 13, 10, 0, 0, 123000, tzinfo=UTC),
    }


def test_message_follows_the_kafka_contract() -> None:
    msg = build_message(row(123), "catalog.changes")
    assert msg.topic == "catalog.changes"
    assert msg.key == b"B000123"
    value = json.loads(msg.value or b"")
    assert value == {
        "event_id": "123",
        "event_type": "product.upserted",
        "sku": "B000123",
        "version": 7,
        "outbox_created_at": "2026-10-13T10:00:00.123Z",
        "product": {"sku": "B000123", "title_en": "x", "version": 7},
    }
    headers = dict(msg.headers)
    assert headers["event_id"] == b"123"
    assert headers["sku"] == b"B000123"
    assert headers["version"] == b"7"
    assert headers["attempt"] == b"0"
    # the outbox carrier's trace id is continued (no SDK in unit tests: same span context is propagated)
    assert headers["traceparent"].decode().split("-")[1] == TRACEPARENT.split("-")[1]


def test_payload_without_version_is_published_with_null_version() -> None:
    msg = build_message(row(1, version=None, trace={}), "catalog.changes")
    assert json.loads(msg.value or b"")["version"] is None
    assert "traceparent" not in dict(msg.headers)


def test_string_payload_and_naive_timestamp_are_handled() -> None:
    r = row(5)
    r["payload"] = json.dumps(r["payload"])
    r["created_at"] = datetime(2026, 1, 2, 3, 4, 5)
    value = json.loads(build_message(r, "catalog.changes").value or b"")
    assert value["product"]["version"] == 7
    assert value["outbox_created_at"] == "2026-01-02T03:04:05.000Z"
    assert iso_ms(datetime(2026, 1, 2, tzinfo=UTC)) == "2026-01-02T00:00:00.000Z"


def test_producer_settings_are_idempotent() -> None:
    cfg = producer_config("x:1", "relay")
    assert cfg["enable.idempotence"] is True
    assert cfg["acks"] == "all"
    assert cfg["max.in.flight.requests.per.connection"] == 5


class FakeProducer:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.produced: list[tuple[str, bytes | None]] = []
        self._cbs: list[Any] = []

    def produce(
        self, topic: str, key: Any = None, value: Any = None, headers: Any = None, on_delivery: Any = None
    ) -> None:
        self.produced.append((topic, key))
        self._cbs.append(on_delivery)

    def poll(self, timeout: float = 0) -> int:
        return 0

    def flush(self, timeout: float = 0) -> int:
        class _Err:
            def str(self) -> str:
                return "broker down"

        class _Msg:
            def topic(self) -> str:
                return "catalog.changes"

        for cb in self._cbs:
            cb(_Err() if self.fail else None, _Msg())
        self._cbs = []
        return 0


def test_produce_and_wait_raises_on_failed_delivery() -> None:
    msgs = [OutgoingMessage("t", b"k", b"v", [])]
    produce_and_wait(FakeProducer(), msgs)
    with pytest.raises(DeliveryError):
        produce_and_wait(FakeProducer(fail=True), msgs)


class FakeCursor:
    def __init__(self, db: FakeDb) -> None:
        self.db = db
        self._result: list[dict[str, Any]] = []
        self.rowcount = 0

    def execute(self, query: str, params: dict[str, Any] | None = None) -> FakeCursor:
        if "FOR UPDATE SKIP LOCKED" in query:
            limit = (params or {})["limit"]
            self._result = [r for r in self.db.rows if r["id"] not in self.db.published][:limit]
        elif query == MARK_PUBLISHED:
            self.db.pending_marks.extend((params or {})["ids"])
        elif "count(*)" in query:
            self._result = [{"n": len(self.db.rows) - len(self.db.published), "oldest": None}]
        return self

    def fetchall(self) -> list[dict[str, Any]]:
        return self._result

    def fetchone(self) -> dict[str, Any] | None:
        return self._result[0] if self._result else None

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeDb:
    def __init__(self, n: int) -> None:
        self.rows = [row(i, sku=f"S{i}") for i in range(1, n + 1)]
        self.published: set[int] = set()
        self.pending_marks: list[int] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    @contextmanager
    def transaction(self) -> Any:
        self.pending_marks = []
        try:
            yield
        except BaseException:
            self.rollbacks += 1
            self.pending_marks = []
            raise
        self.published.update(self.pending_marks)
        self.commits += 1

    def cursor(self, row_factory: Any = None) -> FakeCursor:
        return FakeCursor(self)

    def close(self) -> None:
        self.closed = True


def test_publish_batch_marks_only_after_acknowledgement() -> None:
    db = FakeDb(7)
    producer = FakeProducer()
    relay = OutboxRelay(lambda: db, producer, batch_size=5)  # type: ignore[arg-type,return-value]
    assert relay.publish_batch() == 5
    assert db.published == {1, 2, 3, 4, 5}
    assert relay.publish_batch() == 2
    assert relay.publish_batch() == 0
    assert [k for _, k in producer.produced] == [f"S{i}".encode() for i in range(1, 8)]


def test_delivery_failure_rolls_back_so_rows_are_retried() -> None:
    db = FakeDb(3)
    producer = FakeProducer(fail=True)
    relay = OutboxRelay(lambda: db, producer, batch_size=500)  # type: ignore[arg-type,return-value]
    with pytest.raises(DeliveryError):
        relay.publish_batch()
    assert db.published == set()
    assert db.rollbacks == 1
    producer.fail = False
    assert relay.publish_batch() == 3
    assert db.published == {1, 2, 3}


def test_run_loop_stops_cleanly_between_transactions() -> None:
    db = FakeDb(2)
    producer = FakeProducer()
    relay = OutboxRelay(lambda: db, producer, batch_size=500, poll_interval_s=0.01)  # type: ignore[arg-type,return-value]
    calls = {"n": 0}
    original = relay.publish_batch

    def once() -> int:
        calls["n"] += 1
        if calls["n"] >= 2:
            relay.request_stop()
        return original()

    relay.publish_batch = once  # type: ignore[method-assign]
    relay.run()
    assert db.published == {1, 2}
    assert db.closed

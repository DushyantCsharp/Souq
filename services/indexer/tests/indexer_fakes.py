"""In-memory stand-ins for OpenSearch and Kafka used by the indexer unit tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from souq_indexer.events import Envelope, encode_headers, parse_event

Action = tuple[str, dict[str, Any], dict[str, Any] | None]
Behaviour = Callable[[list[Action], "FakeOpenSearch"], dict[str, Any]]


def parse_bulk(body: bytes) -> list[Action]:
    lines = [json.loads(line) for line in body.decode().splitlines() if line.strip()]
    out: list[Action] = []
    i = 0
    while i < len(lines):
        op, meta = next(iter(lines[i].items()))
        if op == "index":
            out.append((op, meta, lines[i + 1]))
            i += 2
        else:
            out.append((op, meta, None))
            i += 1
    return out


class FakeIndices:
    def __init__(self, aliases: dict[str, list[str]]) -> None:
        self.aliases = aliases
        self.calls = 0

    def get_alias(self, name: str, ignore: Any = None) -> dict[str, Any]:
        self.calls += 1
        indices = self.aliases.get(name)
        if not indices:
            return {"error": f"alias [{name}] missing", "status": 404}
        return {idx: {"aliases": {name: {}}} for idx in indices}


class FakeOpenSearch:
    """Applies bulk actions with real external-version semantics unless a scripted behaviour intervenes."""

    def __init__(self, aliases: dict[str, list[str]] | None = None) -> None:
        self.indices = FakeIndices(aliases if aliases is not None else {"products_write": ["products_v1"]})
        self.docs: dict[tuple[str, str], tuple[int, dict[str, Any] | None]] = {}
        self.calls: list[list[Action]] = []
        self.script: list[Behaviour | Exception] = []

    def bulk(self, body: bytes, **_: Any) -> dict[str, Any]:
        actions = parse_bulk(body)
        self.calls.append(actions)
        if self.script:
            step = self.script.pop(0)
            if isinstance(step, Exception):
                raise step
            return step(actions, self)
        return self.apply(actions)

    def apply_one(self, op: str, meta: dict[str, Any], src: dict[str, Any] | None) -> dict[str, Any]:
        key = (meta["_index"], meta["_id"])
        current = self.docs.get(key)
        version = int(meta["version"])
        if current is not None and current[0] >= version:
            return {
                op: {"status": 409, "error": {"type": "version_conflict_engine_exception", "reason": "vc"}}
            }
        if op == "index":
            self.docs[key] = (version, src)
            return {op: {"status": 201 if current is None else 200}}
        self.docs[key] = (version, None)  # tombstone
        return {op: {"status": 200 if current is not None and current[1] is not None else 404}}

    def apply(self, actions: list[Action]) -> dict[str, Any]:
        items = [self.apply_one(op, meta, src) for op, meta, src in actions]
        errors = any(
            next(iter(i.values()))["status"] >= 300 and "error" in next(iter(i.values())) for i in items
        )
        return {"errors": errors, "items": items}

    def live_doc(self, index: str, sku: str) -> tuple[int, dict[str, Any] | None] | None:
        return self.docs.get((index, sku))


def fail_items(
    predicate: Callable[[str, dict[str, Any]], bool], status: int, error_type: str, reason: str = "boom"
) -> Behaviour:
    """Behaviour: matching items fail with ``status``/``error_type``; the rest are applied normally."""

    def step(actions: list[Action], os_: FakeOpenSearch) -> dict[str, Any]:
        items = []
        for op, meta, src in actions:
            if predicate(op, meta):
                items.append({op: {"status": status, "error": {"type": error_type, "reason": reason}}})
            else:
                items.append(os_.apply_one(op, meta, src))
        return {"errors": True, "items": items}

    return step


def make_value(
    sku: str,
    version: int,
    event_type: str = "product.upserted",
    created_at: str = "2026-10-13T10:00:00.000Z",
    event_id: str = "1",
) -> bytes:
    product = None
    if event_type == "product.upserted":
        product = {
            "sku": sku,
            "brand": "Acme",
            "category_path": "electronics.audio",
            "category_levels": ["electronics", "electronics.audio"],
            "price": 10.5,
            "currency": "AED",
            "colour": "Black",
            "size": None,
            "attributes": {"k": "v"},
            "title_en": f"Thing {sku}",
            "title_ar": "شيء",
            "description_en": None,
            "description_ar": None,
            "updated_at": "2026-10-13T10:00:00Z",
            "version": version,
            "unmapped_extra": "dropped",
        }
    return json.dumps(
        {
            "event_id": event_id,
            "event_type": event_type,
            "sku": sku,
            "version": version,
            "outbox_created_at": created_at,
            "product": product,
        }
    ).encode()


def make_env(
    sku: str,
    version: int,
    *,
    event_type: str = "product.upserted",
    topic: str = "catalog.changes",
    partition: int = 0,
    offset: int = 0,
    attempt: int = 0,
    value: bytes | None = None,
    headers: list[tuple[str, bytes]] | None = None,
) -> Envelope:
    raw = value if value is not None else make_value(sku, version, event_type)
    hdrs = (
        headers
        if headers is not None
        else encode_headers([("event_id", "1"), ("sku", sku), ("version", version), ("attempt", attempt)])
    )
    env = Envelope(
        topic=topic,
        partition=partition,
        offset=offset,
        key=sku.encode(),
        value=raw,
        headers=hdrs,
        received_ns=0,
        attempt=attempt,
        original_topic="catalog.changes",
    )
    try:
        env.event = parse_event(raw)
    except Exception as exc:  # EventParseError
        env.parse_error = exc  # type: ignore[assignment]
    return env


NOW_MS = int(datetime(2026, 10, 13, 10, 0, 5, tzinfo=UTC).timestamp() * 1000)


# ------------------------------------------------------------------------------------------- Kafka
@dataclass
class FakeError:
    code_value: int
    is_fatal: bool = False

    def code(self) -> int:
        return self.code_value

    def fatal(self) -> bool:
        return self.is_fatal

    def str(self) -> str:
        return f"error {self.code_value}"


@dataclass
class FakeMessage:
    _topic: str
    _partition: int
    _offset: int
    _key: bytes | None
    _value: bytes | None
    _headers: list[tuple[str, bytes]] = field(default_factory=list)
    _error: FakeError | None = None

    def topic(self) -> str:
        return self._topic

    def partition(self) -> int:
        return self._partition

    def offset(self) -> int:
        return self._offset

    def key(self) -> bytes | None:
        return self._key

    def value(self) -> bytes | None:
        return self._value

    def headers(self) -> list[tuple[str, bytes]]:
        return self._headers

    def error(self) -> FakeError | None:
        return self._error

    def timestamp(self) -> tuple[int, int]:
        return (1, NOW_MS)


class FakeConsumer:
    """Single-member consumer over in-memory partitions with pause/seek/commit bookkeeping."""

    def __init__(self, partitions: dict[tuple[str, int], list[FakeMessage]]) -> None:
        self.logs = partitions
        self.positions: dict[tuple[str, int], int] = dict.fromkeys(partitions, 0)
        self.paused: set[tuple[str, int]] = set()
        self.commit_map: dict[tuple[str, int], int] = {}
        self.commits: list[list[tuple[str, int, int]]] = []
        self.seeks: list[tuple[str, int, int]] = []
        self.closed = False
        self._on_assign: Any = None

    def subscribe(
        self, topics: list[str], on_assign: Any = None, on_revoke: Any = None, on_lost: Any = None
    ) -> None:
        self._on_assign = on_assign

    def assign_all(self) -> None:
        from confluent_kafka import TopicPartition

        if self._on_assign is not None:
            self._on_assign(self, [TopicPartition(t, p) for t, p in self.logs])

    def assignment(self) -> list[Any]:
        from confluent_kafka import TopicPartition

        return [TopicPartition(t, p) for t, p in self.logs]

    def consume(self, num_messages: int = 1, timeout: float = 0) -> list[FakeMessage]:
        out: list[FakeMessage] = []
        for tp, log in self.logs.items():
            if tp in self.paused:
                continue
            while self.positions[tp] < len(log) and len(out) < num_messages:
                out.append(log[self.positions[tp]])
                self.positions[tp] += 1
        return out

    def poll(self, timeout: float = 0) -> FakeMessage | None:
        msgs = self.consume(1, timeout)
        return msgs[0] if msgs else None

    def pause(self, parts: list[Any]) -> None:
        self.paused.update((p.topic, p.partition) for p in parts)

    def resume(self, parts: list[Any]) -> None:
        for p in parts:
            self.paused.discard((p.topic, p.partition))

    def seek(self, part: Any) -> None:
        self.seeks.append((part.topic, part.partition, part.offset))
        self.positions[(part.topic, part.partition)] = part.offset

    def commit(self, offsets: list[Any], asynchronous: bool = True) -> None:
        self.commits.append([(p.topic, p.partition, p.offset) for p in offsets])
        for p in offsets:
            self.commit_map[(p.topic, p.partition)] = p.offset

    def get_watermark_offsets(self, part: Any, timeout: float = 0, cached: bool = False) -> tuple[int, int]:
        return 0, len(self.logs.get((part.topic, part.partition), []))

    def committed(self, parts: list[Any], timeout: float = 0) -> list[Any]:
        from confluent_kafka import TopicPartition

        return [
            TopicPartition(p.topic, p.partition, self.commit_map.get((p.topic, p.partition), -1001))
            for p in parts
        ]

    def list_topics(self, topic: str, timeout: float = 0) -> Any:
        class _T:
            def __init__(self) -> None:
                self.error = None
                self.partitions = {0: None, 1: None, 2: None}

        class _M:
            def __init__(self) -> None:
                self.topics = {topic: _T()}

        return _M()

    def close(self) -> None:
        self.closed = True


class FakeProducer:
    def __init__(self, fail: bool = False) -> None:
        self.produced: list[dict[str, Any]] = []
        self.fail = fail
        self._pending: list[Any] = []

    def produce(
        self, topic: str, key: Any = None, value: Any = None, headers: Any = None, on_delivery: Any = None
    ) -> None:
        self.produced.append({"topic": topic, "key": key, "value": value, "headers": list(headers or [])})
        self._pending.append((on_delivery, topic))

    def poll(self, timeout: float = 0) -> int:
        return 0

    def flush(self, timeout: float = 0) -> int:
        for cb, topic in self._pending:
            msg = FakeMessage(topic, 0, 0, None, None)
            if cb is not None:
                cb(FakeError(-1) if self.fail else None, msg)
        self._pending = []
        return 0

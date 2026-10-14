"""``souq-dlq`` — inspect and redrive ``catalog.dlq``.

    souq-dlq list    [--sku-from A] [--sku-to B] [--error-class C] [--limit N] [--json]
    souq-dlq inspect (--at PARTITION:OFFSET | --sku SKU)
    souq-dlq redrive (--sku-from A | --sku-to B | --all) [--error-class C] [--dry-run]

Redrive republishes the original value to ``catalog.changes`` keyed by sku, keeping the trace/event
headers and resetting ``attempt`` to 0. It is always safe to redrive: indexing uses external versions,
so a DLQ entry that has since been superseded by a newer version is a 409 (no-op) in OpenSearch.
The DLQ itself is append-only; ``list`` shows everything still retained.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TextIO

from confluent_kafka import Consumer, KafkaError, KafkaException, TopicPartition

from souq_common.settings import get_settings
from souq_indexer.events import (
    H_ATTEMPT,
    H_ERROR_CLASS,
    H_ERROR_MESSAGE,
    H_ORIGINAL_TOPIC,
    H_SKU,
    H_VERSION,
    ROUTING_HEADERS,
    Headers,
    encode_headers,
    header_map,
    normalise_headers,
)
from souq_indexer.kafka_io import OutgoingMessage, make_producer, produce_and_wait
from souq_indexer.routing import TOPIC_CHANGES, TOPIC_DLQ


@dataclass(frozen=True, slots=True)
class DlqRecord:
    partition: int
    offset: int
    timestamp_ms: int
    key: bytes | None
    value: bytes | None
    headers: Headers

    @property
    def meta(self) -> dict[str, str]:
        return header_map(self.headers)

    @property
    def sku(self) -> str:
        if self.key is not None:
            return self.key.decode("utf-8", "replace")
        return self.meta.get(H_SKU, "")

    def payload(self) -> Any:
        if self.value is None:
            return None
        try:
            return json.loads(self.value)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self.value.decode("utf-8", "replace")

    def summary(self) -> dict[str, Any]:
        meta = self.meta
        return {
            "at": f"{self.partition}:{self.offset}",
            "timestamp": datetime.fromtimestamp(self.timestamp_ms / 1000, UTC).isoformat(timespec="seconds"),
            "sku": self.sku,
            "version": meta.get(H_VERSION),
            "attempt": meta.get(H_ATTEMPT),
            "original_topic": meta.get(H_ORIGINAL_TOPIC),
            "error_class": meta.get(H_ERROR_CLASS),
            "error_message": meta.get(H_ERROR_MESSAGE),
        }


def in_range(sku: str, sku_from: str | None, sku_to: str | None) -> bool:
    """Inclusive lexicographic range; a missing bound is open."""
    return (sku_from is None or sku >= sku_from) and (sku_to is None or sku <= sku_to)


def redrive_message(record: DlqRecord) -> OutgoingMessage:
    kept = [(k, v) for k, v in record.headers if k not in ROUTING_HEADERS]
    key = record.key if record.key is not None else (record.sku.encode() if record.sku else None)
    return OutgoingMessage(TOPIC_CHANGES, key, record.value, kept + encode_headers([(H_ATTEMPT, 0)]))


class DlqReader:
    """Reads the whole retained DLQ without joining a group or committing anything."""

    def __init__(self, bootstrap: str) -> None:
        self._consumer = Consumer(
            {
                "bootstrap.servers": bootstrap,
                # Unique, never-committed group: reading must not disturb anyone's offsets.
                "group.id": f"souq-dlq-admin-{uuid.uuid4().hex[:8]}",
                "enable.auto.commit": False,
                "enable.partition.eof": True,
            }
        )

    def close(self) -> None:
        self._consumer.close()

    def __iter__(self) -> Iterator[DlqRecord]:
        meta = self._consumer.list_topics(TOPIC_DLQ, timeout=10)
        topic = meta.topics.get(TOPIC_DLQ)
        if topic is None or topic.error is not None:
            raise SystemExit(f"topic {TOPIC_DLQ} not found")
        ends: dict[int, int] = {}
        assignment: list[TopicPartition] = []
        for pid in sorted(topic.partitions):
            low, high = self._consumer.get_watermark_offsets(TopicPartition(TOPIC_DLQ, pid), timeout=10)
            if high > low:
                ends[pid] = high
                assignment.append(TopicPartition(TOPIC_DLQ, pid, low))
        if not assignment:
            return
        self._consumer.assign(assignment)
        while ends:
            msg = self._consumer.poll(5.0)
            if msg is None:
                break  # watermarks moved backwards (retention) or broker unreachable: stop, don't spin
            err = msg.error()
            if err is not None:
                if err.code() == KafkaError._PARTITION_EOF:
                    ends.pop(msg.partition() or 0, None)
                    continue
                raise KafkaException(err)
            pid = msg.partition() or 0
            offset = msg.offset() or 0
            if pid not in ends:
                continue
            yield DlqRecord(
                partition=pid,
                offset=offset,
                timestamp_ms=msg.timestamp()[1],
                key=msg.key() if isinstance(msg.key(), bytes) else None,
                value=msg.value() if isinstance(msg.value(), bytes) else None,
                headers=normalise_headers(msg.headers()),
            )
            if offset + 1 >= ends[pid]:
                ends.pop(pid)


def _matches(record: DlqRecord, args: argparse.Namespace) -> bool:
    if not in_range(record.sku, args.sku_from, args.sku_to):
        return False
    return not (args.error_class and record.meta.get(H_ERROR_CLASS) != args.error_class)


def cmd_list(records: Iterator[DlqRecord], args: argparse.Namespace, out: TextIO) -> int:
    shown = 0
    for record in records:
        if not _matches(record, args):
            continue
        s = record.summary()
        if args.json:
            out.write(json.dumps(s, ensure_ascii=False) + "\n")
        else:
            msg = (s["error_message"] or "").replace("\n", " ")
            out.write(
                f"{s['at']:>10}  {s['timestamp']}  {s['sku']:<20}  v{s['version'] or '?':<6} "
                f"attempt={s['attempt'] or '?'}  {s['error_class'] or '-'}: {msg[:100]}\n"
            )
        shown += 1
        if args.limit and shown >= args.limit:
            break
    if not args.json:
        out.write(f"{shown} message(s)\n")
    return 0


def cmd_inspect(records: Iterator[DlqRecord], args: argparse.Namespace, out: TextIO) -> int:
    want_at: tuple[int, int] | None = None
    if args.at:
        p, _, o = args.at.partition(":")
        want_at = (int(p), int(o))
    found = 0
    for record in records:
        if want_at is not None and (record.partition, record.offset) != want_at:
            continue
        if args.sku is not None and record.sku != args.sku:
            continue
        meta = record.meta
        detail = {
            "topic": TOPIC_DLQ,
            "partition": record.partition,
            "offset": record.offset,
            "timestamp": record.summary()["timestamp"],
            "key": record.sku,
            "headers": meta,
            "error": {"class": meta.get(H_ERROR_CLASS), "message": meta.get(H_ERROR_MESSAGE)},
            "payload": record.payload(),
        }
        out.write(json.dumps(detail, ensure_ascii=False, indent=2) + "\n")
        found += 1
        if want_at is not None:
            break
    if not found:
        out.write("no matching DLQ message\n")
        return 1
    return 0


def cmd_redrive(
    records: Iterator[DlqRecord], args: argparse.Namespace, out: TextIO, producer: Any | None
) -> int:
    if not (args.all or args.sku_from or args.sku_to):
        out.write("refusing to redrive without --sku-from/--sku-to (or --all)\n")
        return 2
    batch: list[OutgoingMessage] = []
    total = 0
    for record in records:
        if not _matches(record, args):
            continue
        total += 1
        s = record.summary()
        prefix = "would redrive" if args.dry_run else "redrive"
        out.write(f"{prefix} {s['at']} sku={s['sku']} v{s['version']} ({s['error_class']})\n")
        if not args.dry_run:
            batch.append(redrive_message(record))
            if len(batch) >= 500:
                produce_and_wait(producer, batch)
                batch = []
    if batch and not args.dry_run:
        produce_and_wait(producer, batch)
    verb = "would be redriven (dry run)" if args.dry_run else f"redriven to {TOPIC_CHANGES}"
    out.write(f"{total} message(s) {verb}\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="souq-dlq", description="Inspect and redrive catalog.dlq")
    parser.add_argument("--bootstrap", default=None, help="Kafka bootstrap (default: KAFKA_BOOTSTRAP)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_filters(p: argparse.ArgumentParser) -> None:
        p.add_argument("--sku-from", default=None, help="inclusive lower bound (lexicographic)")
        p.add_argument("--sku-to", default=None, help="inclusive upper bound (lexicographic)")
        p.add_argument("--error-class", default=None)

    p_list = sub.add_parser("list", help="list DLQ messages")
    add_filters(p_list)
    p_list.add_argument("--limit", type=int, default=100)
    p_list.add_argument("--json", action="store_true")

    p_inspect = sub.add_parser("inspect", help="show headers, error and payload")
    group = p_inspect.add_mutually_exclusive_group(required=True)
    group.add_argument("--at", help="PARTITION:OFFSET")
    group.add_argument("--sku")

    p_redrive = sub.add_parser("redrive", help="republish to catalog.changes with attempt reset")
    add_filters(p_redrive)
    p_redrive.add_argument("--all", action="store_true", help="redrive every retained message")
    p_redrive.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    bootstrap = args.bootstrap or get_settings().kafka_bootstrap
    reader = DlqReader(bootstrap)
    try:
        if args.cmd == "list":
            return cmd_list(iter(reader), args, sys.stdout)
        if args.cmd == "inspect":
            return cmd_inspect(iter(reader), args, sys.stdout)
        producer = None if args.dry_run else make_producer(bootstrap, "souq-dlq-admin")
        return cmd_redrive(iter(reader), args, sys.stdout, producer)
    finally:
        reader.close()


if __name__ == "__main__":
    raise SystemExit(main())

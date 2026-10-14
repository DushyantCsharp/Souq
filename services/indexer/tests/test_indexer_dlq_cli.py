"""souq-dlq: listing, inspection, sku-range redrive (attempt reset) and --dry-run."""

from __future__ import annotations

import io
import json

from indexer_fakes import FakeProducer, make_value

from souq_indexer.dlq_cli import (
    DlqRecord,
    build_parser,
    cmd_inspect,
    cmd_list,
    cmd_redrive,
    in_range,
    redrive_message,
)
from souq_indexer.events import encode_headers, header_map


def record(offset: int, sku: str, error_class: str = "mapper_parsing_exception") -> DlqRecord:
    headers = encode_headers(
        [
            ("traceparent", "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"),
            ("event_id", str(offset)),
            ("sku", sku),
            ("version", 3),
            ("attempt", 3),
            ("original_topic", "catalog.changes"),
            ("not_before", 123),
            ("error_class", error_class),
            ("error_message", "failed to parse field [price]"),
        ]
    )
    return DlqRecord(0, offset, 1_760_000_000_000, sku.encode(), make_value(sku, 3), headers)


RECORDS = [record(0, "A100"), record(1, "B200", "invalid_json"), record(2, "C300")]


def test_in_range_is_inclusive_and_open_ended() -> None:
    assert in_range("B", "A", "C")
    assert in_range("A", "A", "A")
    assert not in_range("D", "A", "C")
    assert in_range("Z", "A", None)
    assert in_range("0", None, "A")


def test_redrive_resets_attempt_and_strips_routing_headers() -> None:
    out = redrive_message(RECORDS[0])
    assert out.topic == "catalog.changes"
    assert out.key == b"A100"
    assert out.value == RECORDS[0].value
    headers = header_map(out.headers)
    assert headers["attempt"] == "0"
    for gone in ("not_before", "error_class", "error_message", "original_topic"):
        assert gone not in headers
    assert headers["traceparent"].startswith("00-0af7")
    assert headers["event_id"] == "0"


def test_list_filters_by_sku_range_and_error_class() -> None:
    args = build_parser().parse_args(["list", "--sku-from", "B", "--json"])
    buf = io.StringIO()
    cmd_list(iter(RECORDS), args, buf)
    rows = [json.loads(line) for line in buf.getvalue().splitlines()]
    assert [r["sku"] for r in rows] == ["B200", "C300"]
    assert rows[0]["error_class"] == "invalid_json"

    args = build_parser().parse_args(["list", "--error-class", "invalid_json"])
    buf = io.StringIO()
    cmd_list(iter(RECORDS), args, buf)
    assert "B200" in buf.getvalue()
    assert "A100" not in buf.getvalue()
    assert buf.getvalue().strip().endswith("1 message(s)")


def test_inspect_shows_headers_error_and_payload() -> None:
    args = build_parser().parse_args(["inspect", "--at", "0:1"])
    buf = io.StringIO()
    assert cmd_inspect(iter(RECORDS), args, buf) == 0
    detail = json.loads(buf.getvalue())
    assert detail["key"] == "B200"
    assert detail["error"] == {"class": "invalid_json", "message": "failed to parse field [price]"}
    assert detail["headers"]["version"] == "3"
    assert detail["payload"]["sku"] == "B200"

    args = build_parser().parse_args(["inspect", "--sku", "NOPE"])
    assert cmd_inspect(iter(RECORDS), args, io.StringIO()) == 1


def test_redrive_dry_run_produces_nothing() -> None:
    producer = FakeProducer()
    args = build_parser().parse_args(["redrive", "--sku-from", "A100", "--sku-to", "B999", "--dry-run"])
    buf = io.StringIO()
    assert cmd_redrive(iter(RECORDS), args, buf, producer) == 0
    assert producer.produced == []
    assert "2 message(s) would be redriven (dry run)" in buf.getvalue()


def test_redrive_republishes_matching_range() -> None:
    producer = FakeProducer()
    args = build_parser().parse_args(["redrive", "--sku-from", "B000", "--sku-to", "C999"])
    assert cmd_redrive(iter(RECORDS), args, io.StringIO(), producer) == 0
    assert [p["key"] for p in producer.produced] == [b"B200", b"C300"]
    assert {p["topic"] for p in producer.produced} == {"catalog.changes"}


def test_redrive_requires_a_range_or_all() -> None:
    args = build_parser().parse_args(["redrive"])
    assert cmd_redrive(iter(RECORDS), args, io.StringIO(), FakeProducer()) == 2

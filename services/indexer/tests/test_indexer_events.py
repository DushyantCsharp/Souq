"""Message parsing, header helpers and document building."""

from __future__ import annotations

import json

import numpy as np
import pytest
from indexer_fakes import FakeOpenSearch, make_value

from souq_indexer.aliases import NoWriteIndexError, WriteAliasResolver
from souq_indexer.documents import attach_embeddings, build_document
from souq_indexer.events import (
    EventParseError,
    encode_headers,
    header_int,
    header_map,
    normalise_headers,
    parse_event,
    routed_headers,
)


def test_parse_event_accepts_the_contract_shape() -> None:
    event = parse_event(make_value("B000123", 7))
    assert (event.sku, event.version, event.event_type) == ("B000123", 7, "product.upserted")
    assert event.outbox_created_at.year == 2026


def test_parse_event_coerces_numeric_event_id() -> None:
    raw = json.loads(make_value("S", 1))
    raw["event_id"] = 123
    assert parse_event(json.dumps(raw).encode()).event_id == "123"


@pytest.mark.parametrize(
    ("value", "error_class"),
    [
        (None, "empty_value"),
        (b"{nope", "invalid_json"),
        (b'{"event_id": "1"}', "invalid_event"),
        (json.dumps({**json.loads(make_value("S", 1)), "version": None}).encode(), "invalid_event"),
        (json.dumps({**json.loads(make_value("S", 1)), "product": None}).encode(), "invalid_event"),
    ],
)
def test_parse_event_errors_are_classified(value: bytes | None, error_class: str) -> None:
    with pytest.raises(EventParseError) as exc:
        parse_event(value)
    assert exc.value.error_class == error_class


def test_delete_events_need_no_product() -> None:
    event = parse_event(make_value("S", 4, event_type="product.deleted"))
    assert event.product is None


def test_routed_headers_replace_previous_hop() -> None:
    original = encode_headers(
        [("traceparent", "tp"), ("attempt", 1), ("not_before", 5), ("error_class", "old"), ("sku", "S")]
    )
    out = header_map(
        routed_headers(
            original,
            attempt=2,
            not_before_ms=99,
            original_topic="catalog.changes",
            error_class="new",
            error_message="x" * 5000,
        )
    )
    assert out["attempt"] == "2"
    assert out["not_before"] == "99"
    assert out["error_class"] == "new"
    assert out["traceparent"] == "tp"
    assert len(out["error_message"].encode()) <= 2048 + 3


def test_header_helpers_tolerate_odd_input() -> None:
    assert normalise_headers(None) == []
    assert normalise_headers([("a", b"1"), ("b", None)]) == [("a", b"1")]
    assert header_int({"attempt": "x"}, "attempt", 4) == 4
    assert header_int({}, "attempt", 0) == 0


def test_build_document_whitelists_mapped_fields() -> None:
    doc = build_document(parse_event(make_value("S", 2)))
    assert "unmapped_extra" not in doc
    assert "size" not in doc  # None values are omitted
    assert doc["version"] == 2
    assert doc["colour"] == "black"


def test_build_document_derives_category_levels_when_absent() -> None:
    raw = json.loads(make_value("S", 2))
    del raw["product"]["category_levels"]
    raw["product"]["category_path"] = "a.b > c"
    doc = build_document(parse_event(json.dumps(raw).encode()))
    assert doc["category_levels"] == ["a.b", "a.b > c"]


class ZeroEmbedder:
    dim = 4

    def encode_documents(self, texts: list[str]) -> np.ndarray:
        out = np.ones((len(texts), 4), dtype=np.float32)
        out[0] = 0.0
        return out

    def encode_queries(self, texts: list[str]) -> np.ndarray:
        return self.encode_documents(texts)


def test_zero_vectors_are_not_sent_to_a_cosine_field() -> None:
    docs = [{"title_en": "a"}, {"title_en": "b"}]
    attach_embeddings(docs, ZeroEmbedder())
    assert "embedding" not in docs[0]
    assert docs[1]["embedding"] == [1.0, 1.0, 1.0, 1.0]


def test_alias_resolver_refreshes_and_reports_missing_alias() -> None:
    client = FakeOpenSearch({"products_write": ["products_v2", "products_v1"]})
    now = [0.0]
    resolver = WriteAliasResolver(client, "products_write", refresh_s=10, clock=lambda: now[0])
    assert resolver.indices() == ("products_v1", "products_v2")
    client.indices.aliases["products_write"] = ["products_v2"]
    now[0] = 5
    assert resolver.indices() == ("products_v1", "products_v2")  # cached
    now[0] = 10
    assert resolver.indices() == ("products_v2",)
    client.indices.aliases.clear()
    resolver.invalidate()
    with pytest.raises(NoWriteIndexError):
        resolver.indices()

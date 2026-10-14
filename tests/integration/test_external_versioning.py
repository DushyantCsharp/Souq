"""Out-of-order events cannot regress a document: the indexer writes with ``version_type=external`` and
treats the resulting 409 as success (not a retry, not a DLQ entry).

Events are produced straight to ``catalog.changes`` so the test controls versions and ordering. To know
the stale event has been *processed* (not merely not-yet-consumed), a barrier event for another SKU is
produced to the same partition after it: per-partition order means the barrier becoming visible implies
the stale event was handled first.
"""

from __future__ import annotations

import uuid

import pytest
from souq_testing.factories import change_event, make_product
from souq_testing.services import Stack
from souq_testing.streams import (
    os_alias_indices,
    os_get_doc,
    produce_events,
    read_topic,
    wait_for,
    wait_for_doc,
)

pytestmark = pytest.mark.integration

WAIT_S = 90


def eid() -> str:
    return uuid.uuid4().hex


def assert_never_routed(stack: Stack, sku: str) -> None:
    for topic in ("catalog.retry.30s", "catalog.retry.5m", "catalog.dlq"):
        routed = [r for r in read_topic(stack.kafka_bootstrap, topic) if r["key"] == sku]
        assert routed == [], f"{sku} was routed to {topic}: {routed[:1]}"


def test_stale_version_does_not_overwrite_newer(stack: Stack, token: str, sku_prefix: str) -> None:
    base = make_product(1, prefix=sku_prefix)
    v3 = base.with_(title_en=f"{token} version three")
    v2 = base.with_(title_en=f"{token} version two stale", price=base.price - 50)

    [delivered] = produce_events(stack.kafka_bootstrap, [change_event(v3, 3, event_id=eid())])
    doc = wait_for_doc(stack.opensearch_url, "products_read", base.sku, WAIT_S, min_version=3)
    assert doc["_version"] == 3
    assert doc["_source"]["title_en"] == v3.title_en

    produce_events(
        stack.kafka_bootstrap, [change_event(v2, 2, event_id=eid())], partition=delivered.partition
    )
    barrier = make_product(2, prefix=sku_prefix)
    produce_events(
        stack.kafka_bootstrap, [change_event(barrier, 1, event_id=eid())], partition=delivered.partition
    )
    wait_for_doc(stack.opensearch_url, "products_read", barrier.sku, WAIT_S)

    # Every index behind products_write (dual-write during a reindex) must hold v3.
    for index in os_alias_indices(stack.opensearch_url, "products_write"):
        current = os_get_doc(stack.opensearch_url, index, base.sku)
        assert current is not None, f"{base.sku} missing from {index}"
        assert current["_version"] == 3, f"{index} regressed to {current['_version']}"
        assert current["_source"]["title_en"] == v3.title_en
        assert float(current["_source"]["price"]) == pytest.approx(float(v3.price))
    assert_never_routed(stack, base.sku)


def test_redelivered_same_version_is_a_noop(stack: Stack, sku_prefix: str) -> None:
    p = make_product(3, prefix=sku_prefix)
    event = change_event(p, 5, event_id=eid())
    [delivered] = produce_events(stack.kafka_bootstrap, [event])
    wait_for_doc(stack.opensearch_url, "products_read", p.sku, WAIT_S, min_version=5)

    # At-least-once delivery: the very same event again (e.g. relay crash before marking published).
    produce_events(stack.kafka_bootstrap, [event], partition=delivered.partition)
    barrier = make_product(4, prefix=sku_prefix)
    produce_events(
        stack.kafka_bootstrap, [change_event(barrier, 1, event_id=eid())], partition=delivered.partition
    )
    wait_for_doc(stack.opensearch_url, "products_read", barrier.sku, WAIT_S)

    current = os_get_doc(stack.opensearch_url, "products_read", p.sku)
    assert current is not None
    assert current["_version"] == 5
    assert_never_routed(stack, p.sku)


def test_stale_upsert_cannot_resurrect_a_deleted_product(stack: Stack, sku_prefix: str) -> None:
    p = make_product(5, prefix=sku_prefix)
    [delivered] = produce_events(stack.kafka_bootstrap, [change_event(p, 3, event_id=eid())])
    wait_for_doc(stack.opensearch_url, "products_read", p.sku, WAIT_S, min_version=3)

    produce_events(
        stack.kafka_bootstrap,
        [change_event(p, 4, event_id=eid(), event_type="product.deleted")],
        partition=delivered.partition,
    )
    wait_for(
        lambda: os_get_doc(stack.opensearch_url, "products_read", p.sku) is None, WAIT_S, f"{p.sku} deleted"
    )

    # v3 arrives late (e.g. from a retry topic): the v4 tombstone wins.
    produce_events(stack.kafka_bootstrap, [change_event(p, 3, event_id=eid())], partition=delivered.partition)
    barrier = make_product(6, prefix=sku_prefix)
    produce_events(
        stack.kafka_bootstrap, [change_event(barrier, 1, event_id=eid())], partition=delivered.partition
    )
    wait_for_doc(stack.opensearch_url, "products_read", barrier.sku, WAIT_S)

    assert os_get_doc(stack.opensearch_url, "products_read", p.sku) is None
    assert_never_routed(stack, p.sku)

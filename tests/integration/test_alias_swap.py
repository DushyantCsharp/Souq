"""Zero-downtime reindex: ``reindex.py --to v2`` while searches and catalog writes keep flowing.

During the migration the indexer dual-writes to every index behind ``products_write`` and ``reindex.py``
backfills, verifies, then swaps ``products_read`` in one ``_aliases`` call. A search loop running the whole
time must see no errors and never a partial result set, and every product written mid-migration must end
up in the new index.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from souq_testing.db import upsert_products
from souq_testing.factories import catalog, tagged_catalog
from souq_testing.services import Stack, run_reindex
from souq_testing.streams import os_alias_indices, os_count, os_get_doc, os_refresh, wait_for_doc

pytestmark = [pytest.mark.integration, pytest.mark.slow]


@dataclass
class LoopStats:
    requests: int = 0
    statuses: dict[int, int] = field(default_factory=dict)
    errors: list[tuple[float, str]] = field(default_factory=list)
    degraded: dict[str, int] = field(default_factory=dict)
    partial: list[int] = field(default_factory=list)

    def error(self, detail: str) -> None:
        self.errors.append((time.monotonic(), detail))

    def by_phase(self, start: float, end: float) -> dict[str, int]:
        """Where errors fell relative to the reindex run: a swap bug clusters, an overloaded node does not."""
        phases = {"before": 0, "during": 0, "after": 0}
        for t, _ in self.errors:
            phases["before" if t < start else "during" if t <= end else "after"] += 1
        return phases


# Steady shopper traffic, not a load test: hammering a single small test node during the backfill would
# measure the node, not the swap.
PACE_S = 0.05


def search_loop(
    base_url: str, token: str, expected: set[str], stop: threading.Event, stats: LoopStats
) -> None:
    queries = [(token, "en"), (token, "ar"), ("headphones", "en"), ("سماعات", "ar")]
    with httpx.Client(timeout=10) as client:
        i = 0
        while not stop.wait(PACE_S):
            q, lang = queries[i % len(queries)]
            i += 1
            try:
                resp = client.post(
                    base_url + "/search",
                    json={"q": q, "lang": lang, "limit": 50, "options": {"llm": False, "rerank": False}},
                )
            except httpx.HTTPError as exc:
                stats.error(f"{type(exc).__name__}: {exc}")
                continue
            stats.requests += 1
            stats.statuses[resp.status_code] = stats.statuses.get(resp.status_code, 0) + 1
            if resp.status_code != 200:
                stats.error(f"{resp.status_code}: {resp.text[:200]}")
                continue
            body: dict[str, Any] = resp.json()
            for reason in body["degraded"]:
                stats.degraded[reason] = stats.degraded.get(reason, 0) + 1
            if q == token:
                got = {item["sku"] for item in body["items"]}
                if not expected <= got:
                    stats.partial.append(len(expected & got))


def writer_loop(
    database_url: str, token: str, prefix: str, stop: threading.Event, written: list[str]
) -> None:
    """``token`` must differ from the search loop's: new matches would push seeded SKUs out of its top 50."""
    n = 0
    while not stop.is_set():
        p = catalog(1, start=50_000 + n, prefix=prefix)[0]
        p = p.with_(title_en=f"{token} {p.title_en}")
        upsert_products(database_url, [p])
        written.append(p.sku)
        n += 1
        stop.wait(0.5)


def test_alias_swap_mid_traffic_has_no_errors(
    stack: Stack, token: str, sku_prefix: str, external_mode: bool
) -> None:
    if external_mode and not (os.environ.get("CI") or os.environ.get("SOUQ_TEST_ALLOW_REINDEX")):
        pytest.skip("migrates the shared products_* aliases; only on disposable CI services")
    read = os_alias_indices(stack.opensearch_url, "products_read")
    if read != ["products_v1"]:
        pytest.skip(f"needs products_read -> products_v1, found {read}")

    seeded = tagged_catalog(30, token, prefix=sku_prefix)
    upsert_products(stack.database_url, seeded)
    expected = {p.sku for p in seeded}
    for p in seeded:
        wait_for_doc(stack.opensearch_url, "products_read", p.sku, 90)
    os_refresh(stack.opensearch_url, "products_read")

    stop = threading.Event()
    stats = LoopStats()
    written: list[str] = []
    threads = [
        threading.Thread(
            target=search_loop, args=(stack.search.base_url, token, expected, stop, stats), daemon=True
        ),
        threading.Thread(
            target=writer_loop,
            args=(stack.database_url, token[::-1], sku_prefix + "W", stop, written),
            daemon=True,
        ),
    ]
    for t in threads:
        t.start()
    try:
        time.sleep(2.0)  # traffic before the migration starts
        # Settle (4 s) > the indexer's alias refresh in this suite (2 s), mirroring 12 s > 10 s in production.
        # A small count tolerance: products are being written during verification by design.
        reindex_start = time.monotonic()
        result = run_reindex(["--to", "v2", "--alias-settle-s", "4", "--count-tolerance", "10"], stack.env)
        reindex_end = time.monotonic()
        time.sleep(3.0)  # traffic after the swap, while the indexer drops products_v1 from its write set
    finally:
        stop.set()
        for t in threads:
            t.join(15)

    assert result.returncode == 0, f"reindex --to v2 failed:\n{result.stdout}\n{result.stderr}"
    assert os_alias_indices(stack.opensearch_url, "products_read") == ["products_v2"]
    assert os_alias_indices(stack.opensearch_url, "products_write") == ["products_v2"]

    summary = (
        f"requests={stats.requests} statuses={stats.statuses} degraded={stats.degraded} "
        f"errors_by_phase={stats.by_phase(reindex_start, reindex_end)}"
    )
    assert stats.requests >= 20, f"search loop barely ran: {summary}"
    assert stats.errors == [], f"errors during the swap: {[e for _, e in stats.errors[:3]]} ({summary})"
    assert stats.statuses == {200: stats.requests}, summary
    assert "bm25" not in stats.degraded, summary
    assert "knn" not in stats.degraded, summary
    assert stats.partial == [], (
        f"partial result sets during the swap (found {stats.partial[:5]} of {len(expected)})"
    )

    # Everything written while the migration ran is in the new index (dual-write and/or backfill).
    assert written, "writer never ran"
    for sku in written:
        wait_for_doc(stack.opensearch_url, "products_v2", sku, 60)
    for p in seeded:
        assert os_get_doc(stack.opensearch_url, "products_v2", p.sku) is not None

    # The backfill covered the whole catalog. (Compared by SKU, not by count: other tests produce events
    # straight to Kafka for SKUs that have no Postgres row.)
    import psycopg

    with psycopg.connect(stack.database_url) as conn:
        db_skus = [r[0] for r in conn.execute("SELECT sku FROM products").fetchall()]
    os_refresh(stack.opensearch_url, "products_v2")
    in_v2 = os_count(stack.opensearch_url, "products_v2", {"terms": {"sku": db_skus}})
    assert in_v2 == len(db_skus), f"products_v2 holds {in_v2} of {len(db_skus)} Postgres products"

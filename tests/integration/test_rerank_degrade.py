"""Degrade, don't fail (CLAUDE.md rule 5): a reranker that blows its budget — or is not there at all —
yields the fused order with ``degraded: ["rerank"]``, inside the latency budget.

The slow reranker is injected into the running service (``app.state.components.pipeline.reranker``), so
the request goes through the real HTTP stack, retrieval and timeout handling.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any

import numpy as np
import pytest
from souq_testing.db import upsert_products
from souq_testing.factories import tagged_catalog
from souq_testing.services import Stack
from souq_testing.streams import wait_for, wait_for_doc

from souq_common.budgets import load_budgets
from souq_search.rerank import Reranker, ScoresPairs

pytestmark = pytest.mark.integration

# Far beyond the stage budget (scaled with it when SOUQ_IT_BUDGET_SCALE relaxes budgets).
SLOW_S = max(3.0, float(load_budgets().search.rerank_ms) / 1000 * 25)
Swap = Callable[[ScoresPairs | None], Reranker]


class SlowCrossEncoder:
    """Scores correctly, but far too late."""

    def __init__(self, delay_s: float) -> None:
        self.delay_s = delay_s
        self.calls = 0

    def score(self, query: str, docs: list[str]) -> np.ndarray:
        self.calls += 1
        time.sleep(self.delay_s)
        return np.arange(len(docs), dtype=np.float32)


@pytest.fixture
def seeded(stack: Stack, token: str, sku_prefix: str) -> Iterator[str]:
    products = tagged_catalog(8, token, prefix=sku_prefix)
    upsert_products(stack.database_url, products)
    for p in products:
        wait_for_doc(stack.opensearch_url, "products_read", p.sku, 90)
    skus = {p.sku for p in products}

    def all_found() -> bool:
        items = stack.search_ok(token, lang="en", limit=24, options={"llm": False, "rerank": False})["items"]
        return skus <= {i["sku"] for i in items}

    wait_for(all_found, 30, "seeded products searchable")
    return token


@pytest.fixture
def swap_reranker(stack: Stack) -> Iterator[Swap]:
    pipeline = stack.search.components.pipeline
    original = pipeline.reranker
    holder: dict[str, Reranker] = {}

    def install(model: ScoresPairs | None) -> Reranker:
        holder["r"] = Reranker(model, slots=1)
        pipeline.reranker = holder["r"]
        return holder["r"]

    yield install
    pipeline.reranker = original
    if "r" in holder:
        holder["r"].close()


def timed_search(stack: Stack, q: str) -> tuple[dict[str, Any], float]:
    start = time.perf_counter()
    body = stack.search_ok(q, lang="en", limit=24, options={"llm": False, "rerank": True})
    return body, (time.perf_counter() - start) * 1000


def test_slow_reranker_degrades_within_budget(stack: Stack, seeded: str, swap_reranker: Swap) -> None:
    budgets = load_budgets().search
    slow = SlowCrossEncoder(SLOW_S)
    swap_reranker(slow)

    body, elapsed_ms = timed_search(stack, seeded)

    assert slow.calls == 1, "the reranker was never asked, so the timeout path was not exercised"
    assert "rerank" in body["degraded"]
    items = body["items"]
    assert len(items) >= 2
    assert all(i["rank_source"] == "fused" for i in items)
    # Fused order: scores are RRF scores, non-increasing.
    scores = [i["score"] for i in items]
    assert scores == sorted(scores, reverse=True)
    # The stage gave up at its budget, and the request stayed inside the standard-path budget
    # (plus HTTP/test-harness slack), nowhere near the reranker's 3 s.
    rerank_ms = float(body["timings_ms"].get("rerank", 0.0))
    assert rerank_ms <= float(budgets.rerank_ms) * 1.5 + 25, body["timings_ms"]
    assert elapsed_ms < float(budgets.total_standard_ms) + 500, f"took {elapsed_ms:.0f} ms"
    assert elapsed_ms < SLOW_S * 1000 / 2


def test_busy_reranker_degrades_immediately(stack: Stack, seeded: str, swap_reranker: Swap) -> None:
    # The first request leaves the slow inference running in its only slot; the next one must not queue
    # behind it.
    swap_reranker(SlowCrossEncoder(SLOW_S))
    first, _ = timed_search(stack, seeded)
    assert "rerank" in first["degraded"]
    second, elapsed_ms = timed_search(stack, seeded)
    assert "rerank" in second["degraded"]
    assert elapsed_ms < float(load_budgets().search.total_standard_ms) + 500


def test_missing_reranker_degrades(stack: Stack, seeded: str, swap_reranker: Swap) -> None:
    swap_reranker(None)
    body, _ = timed_search(stack, seeded)
    assert "rerank" in body["degraded"]
    assert body["items"]
    assert all(i["rank_source"] == "fused" for i in body["items"])


def test_healthy_reranker_is_not_degraded(stack: Stack, seeded: str, swap_reranker: Swap) -> None:
    # Control: the same request with a fast reranker reports rerank as the rank source.
    swap_reranker(SlowCrossEncoder(0.0))
    body, _ = timed_search(stack, seeded)
    assert "rerank" not in body["degraded"]
    assert body["items"][0]["rank_source"] == "rerank"

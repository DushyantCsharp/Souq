"""Reranker admission control: a timed-out inference keeps its slot, later calls degrade at once."""

from __future__ import annotations

import asyncio
import time

import pytest
from fakes import FakeCrossEncoder

from souq_search.rerank import Reranker, RerankUnavailable


async def test_scores_in_order() -> None:
    r = Reranker(FakeCrossEncoder())
    scores = await r.score("red shoes", ["red shoes", "blue hat"], timeout_s=1.0)
    assert scores[0] > scores[1]


async def test_timeout_then_busy_then_recovers() -> None:
    r = Reranker(FakeCrossEncoder(delay_s=0.2), slots=1)
    with pytest.raises(TimeoutError):
        await r.score("q", ["d"], timeout_s=0.01)
    t = time.perf_counter()
    with pytest.raises(RerankUnavailable):
        await r.score("q", ["d"], timeout_s=1.0)
    assert time.perf_counter() - t < 0.05
    await asyncio.sleep(0.25)  # the abandoned inference finishes and frees its slot
    assert len(await r.score("q", ["d"], timeout_s=1.0)) == 1


async def test_no_model() -> None:
    with pytest.raises(RerankUnavailable):
        await Reranker(None).score("q", ["d"], timeout_s=1.0)

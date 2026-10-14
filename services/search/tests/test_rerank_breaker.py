"""Reranker circuit breaker: sheds after late attempts, recovers after cool-down."""

from __future__ import annotations

import asyncio
import time

import numpy as np
import pytest

from souq_search.rerank import Reranker, RerankUnavailable


class SlowModel:
    def __init__(self, delay_s: float) -> None:
        self.delay_s = delay_s
        self.calls = 0

    def score(self, query: str, docs: list[str]) -> np.ndarray:
        self.calls += 1
        time.sleep(self.delay_s)
        return np.zeros(len(docs), dtype=np.float32)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def _late_attempt(reranker: Reranker, model: SlowModel) -> None:
    with pytest.raises(TimeoutError):
        await reranker.score("q", ["d"], timeout_s=0.005)
    await asyncio.sleep(model.delay_s * 2)  # let the abandoned inference finish and free its slot


async def test_opens_after_late_attempts_and_skips_inference() -> None:
    model, clock = SlowModel(0.03), Clock()
    reranker = Reranker(model, breaker_window=3, breaker_late_ratio=0.5, cooldown_s=5.0, clock=clock)
    for _ in range(3):
        await _late_attempt(reranker, model)
    assert reranker.shedding
    calls = model.calls
    with pytest.raises(RerankUnavailable, match="shedding"):
        await reranker.score("q", ["d"], timeout_s=1.0)
    assert model.calls == calls  # shed without running the model
    reranker.close()


async def test_recovers_after_cooldown() -> None:
    model, clock = SlowModel(0.03), Clock()
    reranker = Reranker(model, breaker_window=2, cooldown_s=5.0, clock=clock)
    for _ in range(2):
        await _late_attempt(reranker, model)
    assert reranker.shedding
    clock.now += 5.1
    assert not reranker.shedding
    scores = await reranker.score("q", ["a", "b"], timeout_s=1.0)
    assert scores.shape == (2,)
    reranker.close()


async def test_in_time_results_keep_it_closed() -> None:
    model = SlowModel(0.0)
    reranker = Reranker(model, breaker_window=3)
    for _ in range(6):
        await reranker.score("q", ["d"], timeout_s=1.0)
    assert not reranker.shedding
    reranker.close()

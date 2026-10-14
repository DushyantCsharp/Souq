from __future__ import annotations

import math

import pytest

from souq_evals import metrics as m


def test_ndcg_perfect_and_reversed() -> None:
    grades = {"a": 3, "b": 2, "c": 0}
    assert m.ndcg_at_k(["a", "b", "c"], grades) == pytest.approx(1.0)
    worst = m.ndcg_at_k(["c", "b", "a"], grades)
    ideal = (2**3 - 1) / math.log2(2) + (2**2 - 1) / math.log2(3)
    assert worst == pytest.approx(((2**2 - 1) / math.log2(3) + (2**3 - 1) / math.log2(4)) / ideal)


def test_unjudged_counts_as_zero() -> None:
    assert m.ndcg_at_k(["x", "a"], {"a": 3}) == pytest.approx(1 / math.log2(3))


def test_recall_and_mrr() -> None:
    assert m.recall_at_k(["a", "x", "b"], {"a", "b", "c"}, k=2) == pytest.approx(1 / 3)
    assert m.recall_at_k(["a"], set()) is None
    assert m.reciprocal_rank(["x", "y", "a"], {"a"}) == pytest.approx(1 / 3)
    assert m.reciprocal_rank(["x"], {"a"}) == 0.0


def test_mean_skips_none_and_percentile() -> None:
    assert m.mean([1.0, None, 0.0]) == 0.5
    assert m.percentile([1, 2, 3, 4, 100], 50) == 3
    assert m.percentile([1, 2, 3, 4, 100], 95) == 100

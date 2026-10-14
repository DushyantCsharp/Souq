"""Ranking metrics. Unjudged documents count as grade 0 (standard for pooled judgments)."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence


def dcg(gains: Sequence[float]) -> float:
    return sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(gains))


def ndcg_at_k(ranked: Sequence[str], grades: Mapping[str, int], k: int = 10) -> float:
    ideal = dcg(sorted(grades.values(), reverse=True)[:k])
    if ideal == 0:
        return 0.0
    return dcg([grades.get(d, 0) for d in ranked[:k]]) / ideal


def recall_at_k(ranked: Sequence[str], relevant: set[str], k: int = 100) -> float | None:
    """None when the query has no relevant documents (excluded from the mean)."""
    if not relevant:
        return None
    return len(relevant & set(ranked[:k])) / len(relevant)


def reciprocal_rank(ranked: Sequence[str], relevant: set[str]) -> float:
    for i, doc in enumerate(ranked, start=1):
        if doc in relevant:
            return 1.0 / i
    return 0.0


def mean(values: Sequence[float | None]) -> float:
    present = [v for v in values if v is not None]
    return round(sum(present) / len(present), 4) if present else 0.0


def percentile(values: Sequence[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil(p / 100 * len(ordered)) - 1))
    return round(ordered[idx], 2)

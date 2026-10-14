"""Ranking metrics shared by the model benchmark (the eval harness has its own copy-free import of these)."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence


def dcg(gains: Sequence[float]) -> float:
    return sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(gains))


def ndcg_at_k(ranked: Sequence[str], judgments: Mapping[str, int], k: int = 10) -> float:
    ideal = dcg(sorted(judgments.values(), reverse=True)[:k])
    if ideal == 0:
        return 0.0
    return dcg([judgments.get(d, 0) for d in ranked[:k]]) / ideal

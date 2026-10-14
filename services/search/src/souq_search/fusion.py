"""Reciprocal Rank Fusion (Cormack, Clarke & Büttcher 2009): score(d) = Σ_i w_i / (k + rank_i(d)), 1-based."""

from __future__ import annotations

from collections.abc import Sequence


def rrf(
    rankings: Sequence[Sequence[str]],
    k: int = 60,
    weights: Sequence[float] | None = None,
) -> list[tuple[str, float]]:
    """Fuse ranked id lists. Ties break by best single rank, then by id, so output is deterministic."""
    if weights is not None and len(weights) != len(rankings):
        raise ValueError(f"{len(weights)} weights for {len(rankings)} rankings")
    scores: dict[str, float] = {}
    best_rank: dict[str, int] = {}
    for i, ranking in enumerate(rankings):
        weight = 1.0 if weights is None else float(weights[i])
        seen: set[str] = set()
        for rank, doc in enumerate(ranking, start=1):
            # A retriever listing a doc twice must not double its vote.
            if doc in seen:
                continue
            seen.add(doc)
            if weight == 0.0:
                continue
            scores[doc] = scores.get(doc, 0.0) + weight / (k + rank)
            best_rank[doc] = min(best_rank.get(doc, rank), rank)
    return sorted(scores.items(), key=lambda item: (-item[1], best_rank[item[0]], item[0]))

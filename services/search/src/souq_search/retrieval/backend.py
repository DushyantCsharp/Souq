"""Retrieval backend: the four OpenSearch operations the pipeline needs, behind a small protocol so the
pipeline can be unit-tested with an in-memory backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from souq_search.opensearch import OpenSearch, OpenSearchError
from souq_search.retrieval import queries as q


@dataclass
class Hit:
    sku: str
    score: float
    source: dict[str, Any]
    sort: list[Any] | None = None


@dataclass
class RetrievalResult:
    hits: list[Hit]
    total: int
    aggregations: dict[str, Any] | None = None


@dataclass
class TailResult:
    hits: list[Hit]
    pit_id: str
    search_after: list[Any] | None
    exhausted: bool
    extra: dict[str, Any] = field(default_factory=dict)


class Backend(Protocol):
    async def bm25(
        self,
        terms: q.QueryTerms,
        clauses: list[dict[str, Any]],
        size: int,
        aggs: dict[str, Any] | None,
        timeout_s: float,
    ) -> RetrievalResult: ...

    async def knn(
        self, vector: list[float], clauses: list[dict[str, Any]], k: int, timeout_s: float
    ) -> RetrievalResult: ...

    async def fetch(self, skus: list[str], timeout_s: float) -> dict[str, dict[str, Any]]: ...

    async def tail(
        self,
        terms: q.QueryTerms,
        clauses: list[dict[str, Any]],
        exclude: list[str],
        size: int,
        pit_id: str | None,
        search_after: list[Any] | None,
        timeout_s: float,
    ) -> TailResult: ...


def _hits(data: dict[str, Any]) -> list[Hit]:
    out = []
    for h in data.get("hits", {}).get("hits", []):
        src = h.get("_source") or {}
        sku = str(src.get("sku") or h.get("_id"))
        out.append(Hit(sku=sku, score=float(h.get("_score") or 0.0), source=src, sort=h.get("sort")))
    return out


def _total(data: dict[str, Any]) -> int:
    total = data.get("hits", {}).get("total", 0)
    return int(total.get("value", 0)) if isinstance(total, dict) else int(total or 0)


class OpenSearchBackend:
    def __init__(self, client: OpenSearch, index: str, pit_keep_alive: str) -> None:
        self.client = client
        self.index = index
        self.pit_keep_alive = pit_keep_alive

    async def bm25(
        self,
        terms: q.QueryTerms,
        clauses: list[dict[str, Any]],
        size: int,
        aggs: dict[str, Any] | None,
        timeout_s: float,
    ) -> RetrievalResult:
        data = await self.client.search(self.index, q.bm25_body(terms, clauses, size, aggs), timeout_s)
        return RetrievalResult(hits=_hits(data), total=_total(data), aggregations=data.get("aggregations"))

    async def knn(
        self, vector: list[float], clauses: list[dict[str, Any]], k: int, timeout_s: float
    ) -> RetrievalResult:
        data = await self.client.search(self.index, q.knn_body(vector, clauses, k), timeout_s)
        hits = _hits(data)
        return RetrievalResult(hits=hits, total=len(hits))

    async def fetch(self, skus: list[str], timeout_s: float) -> dict[str, dict[str, Any]]:
        if not skus:
            return {}
        data = await self.client.search(self.index, q.docs_body(skus), timeout_s)
        return {h.sku: h.source for h in _hits(data)}

    async def tail(
        self,
        terms: q.QueryTerms,
        clauses: list[dict[str, Any]],
        exclude: list[str],
        size: int,
        pit_id: str | None,
        search_after: list[Any] | None,
        timeout_s: float,
    ) -> TailResult:
        if not pit_id:
            pit_id = await self.client.open_pit(self.index, self.pit_keep_alive, timeout_s)
        body = q.tail_body(terms, clauses, exclude, size, pit_id, self.pit_keep_alive, search_after)
        try:
            data = await self.client.pit_search(body, timeout_s)
        except OpenSearchError as exc:
            if exc.status != 404:
                raise
            # PIT expired: reopen; (score, sku) sort values stay meaningful against the new snapshot.
            pit_id = await self.client.open_pit(self.index, self.pit_keep_alive, timeout_s)
            body["pit"]["id"] = pit_id
            data = await self.client.pit_search(body, timeout_s)
        hits = _hits(data)
        new_pit = str(data.get("pit_id") or pit_id)
        last_sort = hits[-1].sort if hits else search_after
        return TailResult(hits=hits, pit_id=new_pit, search_after=last_sort, exhausted=len(hits) < size)

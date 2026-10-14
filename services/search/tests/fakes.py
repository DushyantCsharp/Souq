"""In-memory stand-ins for OpenSearch, the reranker model, the LLM and the semantic cache."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import numpy as np

from souq_common.arabic import normalise_arabic
from souq_common.embeddings import FakeEmbedder, document_text
from souq_search.retrieval import queries as q
from souq_search.retrieval.backend import Hit, RetrievalResult, TailResult


def _matches(src: dict[str, Any], clause: dict[str, Any]) -> bool:
    if "range" in clause:
        rng = clause["range"]["price"]
        price = float(src.get("price") or 0)
        return price >= rng.get("gte", -1e18) and price <= rng.get("lte", 1e18)
    if "terms" in clause:
        ((field, values),) = clause["terms"].items()
        return str(src.get(field)) in values
    if "term" in clause:
        ((field, value),) = clause["term"].items()
        if isinstance(value, dict):
            value = value["value"]
        if field == "category_levels":
            path = str(src.get("category_path") or "")
            return path == value or path.startswith(value + ".")
        return str(src.get(field)) == value
    if "bool" in clause:
        should = clause["bool"].get("should", [])
        out = []
        for c in should:
            ((field, spec),) = c["term"].items()
            key = field.removesuffix(".kw")
            out.append(str(src.get(key, "")).lower() == str(spec["value"]).lower())
        return any(out)
    raise AssertionError(f"unexpected clause {clause}")


def _tokens(src: dict[str, Any]) -> set[str]:
    text = " ".join(str(src.get(f) or "") for f in ("title_en", "title_ar", "brand"))
    return set(normalise_arabic(text).lower().split())


class FakeBackend:
    def __init__(self, products: list[dict[str, Any]], embedder: FakeEmbedder | None = None) -> None:
        self.products = products
        self.embedder = embedder or FakeEmbedder()
        self.doc_vecs = self.embedder.encode_documents([document_text(p) for p in products])
        self.fail: set[str] = set()
        self.delay_s: dict[str, float] = {}
        self.calls: list[str] = []
        self.pits_opened = 0

    async def _maybe(self, op: str) -> None:
        self.calls.append(op)
        if op in self.delay_s:
            await asyncio.sleep(self.delay_s[op])
        if op in self.fail:
            raise RuntimeError(f"{op} down")

    def _filtered(self, clauses: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [p for p in self.products if all(_matches(p, c) for c in clauses)]

    def _bm25_scored(self, terms: q.QueryTerms, clauses: list[dict[str, Any]]) -> list[Hit]:
        hits = []
        for p in self._filtered(clauses):
            toks = _tokens(p)
            score = sum(1.0 for t in terms.terms if t in toks)
            score += sum(1.0 for t in terms.terms if terms.translit.get(t) in toks)
            if terms.is_empty() or score > 0:
                hits.append(Hit(sku=p["sku"], score=score, source=p, sort=[score, p["sku"]]))
        hits.sort(key=lambda h: (-h.score, h.sku))
        return hits

    async def bm25(
        self,
        terms: q.QueryTerms,
        clauses: list[dict[str, Any]],
        size: int,
        aggs: dict[str, Any] | None,
        timeout_s: float,
    ) -> RetrievalResult:
        await self._maybe("bm25")
        hits = self._bm25_scored(terms, clauses)
        return RetrievalResult(hits=hits[:size], total=len(hits), aggregations=None)

    async def knn(
        self, vector: list[float], clauses: list[dict[str, Any]], k: int, timeout_s: float
    ) -> RetrievalResult:
        await self._maybe("knn")
        allowed = {p["sku"] for p in self._filtered(clauses)}
        sims = self.doc_vecs @ np.asarray(vector, dtype=np.float32)
        order = np.argsort(-sims, kind="stable")
        hits = [
            Hit(sku=self.products[i]["sku"], score=float(sims[i]), source=self.products[i])
            for i in order
            if self.products[i]["sku"] in allowed and sims[i] > 0
        ]
        return RetrievalResult(hits=hits[:k], total=len(hits))

    async def fetch(self, skus: list[str], timeout_s: float) -> dict[str, dict[str, Any]]:
        await self._maybe("fetch")
        by = {p["sku"]: p for p in self.products}
        return {s: by[s] for s in skus if s in by}

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
        await self._maybe("tail")
        if not pit_id:
            self.pits_opened += 1
            pit_id = f"pit-{self.pits_opened}"
        excluded = set(exclude)
        hits = [h for h in self._bm25_scored(terms, clauses) if h.sku not in excluded]
        if search_after:
            s_score, s_sku = search_after
            hits = [h for h in hits if (-h.score, h.sku) > (-s_score, s_sku)]
        page = hits[:size]
        return TailResult(
            hits=page,
            pit_id=pit_id,
            search_after=page[-1].sort if page else search_after,
            exhausted=len(hits) <= size,
        )


class FakeCrossEncoder:
    """Scores = number of query tokens in the doc text, with an optional artificial delay."""

    def __init__(self, delay_s: float = 0.0) -> None:
        self.delay_s = delay_s
        self.calls = 0

    def score(self, query: str, docs: list[str]) -> np.ndarray:
        self.calls += 1
        if self.delay_s:
            time.sleep(self.delay_s)
        qt = set(query.lower().split())
        # reverse-alphabetical tie-break so a rerank visibly changes the fused order
        return np.array(
            [len(qt & set(d.lower().split())) + (ord(d[0]) / 1000 if d else 0) for d in docs],
            dtype=np.float32,
        )


class FakeCache:
    def __init__(self) -> None:
        self.store_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.entries: dict[tuple[str, str], dict[str, Any]] = {}
        self.fail = False

    async def lookup(self, vector: np.ndarray, normalised: str, lang: str) -> dict[str, Any] | None:
        if self.fail:
            raise ConnectionError("redis down")
        return self.entries.get((normalised, lang))

    async def store(self, vector: np.ndarray, normalised: str, lang: str, payload: dict[str, Any]) -> None:
        self.store_calls.append((normalised, lang, payload))
        self.entries[(normalised, lang)] = payload


def catalog() -> list[dict[str, Any]]:
    rows = []
    colours = ["black", "white", "red", "blue"]
    brands = ["Nike", "Adidas", "Puma", "Samsung"]
    for i in range(120):
        brand = brands[i % 4]
        colour = colours[(i // 4) % 4]
        kind = ["running shoes", "backpack", "headphones"][i % 3]
        ar_kind = {"running shoes": "حذاء جري", "backpack": "حقيبة ظهر", "headphones": "سماعات"}[kind]
        rows.append(
            {
                "sku": f"SKU{i:04d}",
                "title_en": f"{brand} {colour} {kind} model {i}",
                "title_ar": f"{ar_kind} {brand} {i}",
                "description_en": f"A {colour} {kind}.",
                "brand": brand,
                "price": 50.0 + (i % 10) * 50,
                "currency": "AED",
                "category_path": {
                    "running shoes": "fashion.shoes",
                    "backpack": "fashion.bags",
                    "headphones": "electronics.audio.headphones",
                }[kind],
                "colour": colour,
                "size": str(38 + i % 8) if kind == "running shoes" else None,
            }
        )
    return rows

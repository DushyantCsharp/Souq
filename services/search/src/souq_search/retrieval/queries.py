"""OpenSearch request bodies: BM25 (term-centric multi-field), kNN (filters inside kNN), facets, tail.

BM25 is built per query term: each term is a ``dis_max`` over the English and Arabic fields (each field's
own analyzer applies), and terms are combined with ``minimum_should_match``. Unlike a per-language
``multi_match`` this works for mixed-script queries ("جراب iphone") where no single field contains every
term. Arabizi terms add their Arabic transliteration (fuzzy, because transliteration drops vowels).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from souq_search.models import Facet, FacetBucket, Filters

SOURCE_FIELDS = [
    "sku",
    "title_en",
    "title_ar",
    "description_en",
    "description_ar",
    "brand",
    "price",
    "currency",
    "category_path",
    "colour",
    "size",
]

# (field, boost) for exact term matches; fuzzy variants get half the boost on titles/brand only.
TERM_FIELDS: list[tuple[str, float]] = [
    ("title_en", 3.0),
    ("title_ar", 3.0),
    ("brand", 2.0),
    ("description_en", 0.5),
    ("description_ar", 0.5),
]
FUZZY_FIELDS: list[tuple[str, float]] = [("title_en", 1.5), ("title_ar", 1.5), ("brand", 1.0)]
MIN_SHOULD_MATCH = "2<75%"
MAX_TERMS = 12

STOPWORDS = {
    "a",
    "an",
    "the",
    "for",
    "with",
    "of",
    "in",
    "on",
    "and",
    "or",
    "to",
    "by",
    "my",
    "me",
    "i",
    "is",
    "are",
    "that",
    "this",
    "some",
    "any",
    "في",
    "من",
    "على",
    "الى",
    "عن",
    "مع",
    "و",
    "ل",
    "ب",
    "او",
    "هذا",
    "هذه",
    "لل",
    "ال",
    "اللي",
    "يلي",
}

PRICE_BUCKETS: list[tuple[float | None, float | None]] = [
    (None, 50),
    (50, 100),
    (100, 250),
    (250, 500),
    (500, 1000),
    (1000, None),
]


@dataclass
class QueryTerms:
    """Retrieval terms: ``terms`` (as typed, normalised) and optional per-term Arabic transliterations."""

    terms: list[str]
    translit: dict[str, str] = field(default_factory=dict)
    phrase: str | None = None
    fuzzy: bool = True
    boosts: list[dict[str, Any]] = field(default_factory=list)  # soft constraints (see boost_clauses)

    def is_empty(self) -> bool:
        return not self.terms


def make_terms(text: str, arabizi_map: dict[str, str] | None = None, fuzzy: bool = True) -> QueryTerms:
    seen: list[str] = []
    for tok in text.split():
        if tok in STOPWORDS or tok in seen:
            continue
        seen.append(tok)
    seen = seen[:MAX_TERMS]
    translit = {t: a for t, a in (arabizi_map or {}).items() if t in seen and a and a != t}
    return QueryTerms(
        terms=seen, translit=translit, phrase=" ".join(seen) if len(seen) > 1 else None, fuzzy=fuzzy
    )


def category_slug(value: str) -> str:
    """Human path ("Electronics > Audio") -> ltree text ("electronics.audio") as stored in category_levels.
    Mirrors packages/db category_ltree(); values already in ltree form pass through."""
    if ">" not in value:
        return value.strip().lower()
    labels = []
    for part in value.split(">"):
        label = part.strip().lower().replace("&", " and ")
        label = re.sub(r"[^a-z0-9]+", "_", label).strip("_")
        if label:
            labels.append(label)
    return ".".join(labels)


def _ci_terms(field_name: str, values: list[str]) -> dict[str, Any]:
    return {
        "bool": {
            "should": [{"term": {field_name: {"value": v, "case_insensitive": True}}} for v in values],
            "minimum_should_match": 1,
        }
    }


def filter_clauses(filters: Filters, *, apply_currency: bool) -> list[dict[str, Any]]:
    clauses: list[dict[str, Any]] = []
    if filters.price_min is not None or filters.price_max is not None:
        rng: dict[str, float] = {}
        if filters.price_min is not None:
            rng["gte"] = filters.price_min
        if filters.price_max is not None:
            rng["lte"] = filters.price_max
        clauses.append({"range": {"price": rng}})
    if apply_currency and filters.currency:
        clauses.append({"term": {"currency": filters.currency}})
    if filters.brands:
        clauses.append(_ci_terms("brand.kw", filters.brands))
    if filters.colours:
        clauses.append({"terms": {"colour": [c.lower() for c in filters.colours]}})
    if filters.sizes:
        clauses.append(_ci_terms("size", filters.sizes))
    if filters.category:
        clauses.append({"term": {"category_levels": category_slug(filters.category)}})
    return clauses


def boost_clauses(filters: Filters) -> list[dict[str, Any]]:
    """Soft constraints: parsed values for sparsely populated fields rank matching products higher
    instead of excluding every product that lacks the attribute."""
    out: list[dict[str, Any]] = []
    if filters.brands:
        out.append({"bool": {**_ci_terms("brand.kw", filters.brands)["bool"], "boost": 2.0}})
    if filters.colours:
        out.append({"terms": {"colour": [c.lower() for c in filters.colours], "boost": 2.0}})
    if filters.sizes:
        out.append({"bool": {**_ci_terms("size", filters.sizes)["bool"], "boost": 1.5}})
    return out


def _term_clause(term: str, translit: str | None, fuzzy: bool) -> dict[str, Any]:
    queries: list[dict[str, Any]] = [
        {"match": {f: {"query": term, "boost": boost}}} for f, boost in TERM_FIELDS
    ]
    if fuzzy and len(term) >= 4 and not term.isdigit():
        queries += [
            {
                "match": {
                    f: {
                        "query": term,
                        "boost": boost,
                        "fuzziness": "AUTO",
                        "prefix_length": 1,
                        "max_expansions": 20,
                    }
                }
            }
            for f, boost in FUZZY_FIELDS
        ]
    if translit:
        queries += [
            {"match": {"title_ar": {"query": translit, "boost": 2.5}}},
            {
                "match": {
                    "title_ar": {
                        "query": translit,
                        "boost": 1.5,
                        "fuzziness": "AUTO",
                        "prefix_length": 1,
                        "max_expansions": 20,
                    }
                }
            },
            {"match": {"description_ar": {"query": translit, "boost": 0.5}}},
        ]
    return {"dis_max": {"queries": queries, "tie_breaker": 0.2}}


def bm25_query(terms: QueryTerms, filters: list[dict[str, Any]]) -> dict[str, Any]:
    boosts = terms.boosts
    if terms.is_empty():
        return {"bool": {"must": [{"match_all": {}}], "filter": filters, "should": boosts or []}}
    should_phrase: list[dict[str, Any]] = []
    if terms.phrase:
        should_phrase = [
            {"match_phrase": {"title_en": {"query": terms.phrase, "slop": 2, "boost": 2.0}}},
            {"match_phrase": {"title_ar": {"query": terms.phrase, "slop": 2, "boost": 2.0}}},
        ]
    return {
        "bool": {
            "must": [
                {
                    "bool": {
                        "should": [_term_clause(t, terms.translit.get(t), terms.fuzzy) for t in terms.terms],
                        "minimum_should_match": MIN_SHOULD_MATCH,
                    }
                }
            ],
            "should": should_phrase + boosts,
            "filter": filters,
        }
    }


def bm25_body(
    terms: QueryTerms,
    filters: list[dict[str, Any]],
    size: int,
    aggs: dict[str, Any] | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "size": size,
        "_source": SOURCE_FIELDS,
        "track_total_hits": 10_000,
        "query": bm25_query(terms, filters),
    }
    if aggs:
        body["aggs"] = aggs
    return body


def knn_body(vector: list[float], filters: list[dict[str, Any]], k: int) -> dict[str, Any]:
    knn: dict[str, Any] = {"vector": vector, "k": k}
    if filters:
        # Filtering inside kNN (lucene engine) keeps k results that satisfy the filter, instead of
        # post-filtering k neighbours down to a handful.
        knn["filter"] = {"bool": {"filter": filters}}
    return {"size": k, "_source": SOURCE_FIELDS, "query": {"knn": {"embedding": knn}}}


def tail_body(
    terms: QueryTerms,
    filters: list[dict[str, Any]],
    exclude_skus: list[str],
    size: int,
    pit_id: str,
    keep_alive: str,
    search_after: list[Any] | None,
) -> dict[str, Any]:
    query = bm25_query(terms, filters)
    if exclude_skus:
        query = {"bool": {"must": [query], "must_not": [{"terms": {"sku": exclude_skus}}]}}
    body: dict[str, Any] = {
        "size": size,
        "_source": SOURCE_FIELDS,
        "query": query,
        "pit": {"id": pit_id, "keep_alive": keep_alive},
        # sku is unique, so (_score, sku) is a total order and search_after never skips or repeats.
        "sort": [{"_score": "desc"}, {"sku": "asc"}],
        "track_total_hits": False,
    }
    if search_after:
        body["search_after"] = search_after
    return body


def docs_body(skus: list[str]) -> dict[str, Any]:
    return {"size": len(skus), "_source": SOURCE_FIELDS, "query": {"terms": {"sku": skus}}}


def facet_aggs(filters: Filters, size: int = 20) -> dict[str, Any]:
    if filters.category:
        # Drill-down: children of the selected node.
        include = re.escape(category_slug(filters.category)) + r"\.[^.]+"
    else:
        include = r"[^.]+(\.[^.]+)?"
    ranges = []
    for lo, hi in PRICE_BUCKETS:
        r: dict[str, Any] = {"key": price_bucket_label(lo, hi)}
        if lo is not None:
            r["from"] = lo
        if hi is not None:
            r["to"] = hi
        ranges.append(r)
    return {
        "brand": {"terms": {"field": "brand.kw", "size": size}},
        "colour": {"terms": {"field": "colour", "size": size}},
        "size": {"terms": {"field": "size", "size": size}},
        "category": {"terms": {"field": "category_levels", "size": size, "include": include}},
        "price": {"range": {"field": "price", "ranges": ranges}},
    }


def price_bucket_label(lo: float | None, hi: float | None) -> str:
    lo_s = "0" if lo is None else f"{lo:g}"
    return f"{lo_s}+" if hi is None else f"{lo_s}-{hi:g}"


FACET_ORDER = ["category", "brand", "price", "colour", "size"]


def parse_facets(aggs: dict[str, Any] | None) -> list[Facet]:
    if not aggs:
        return []
    facets = []
    for name in FACET_ORDER:
        agg = aggs.get(name)
        if not agg:
            continue
        buckets = [
            FacetBucket(value=str(b["key"]), count=int(b["doc_count"]))
            for b in agg.get("buckets", [])
            if int(b.get("doc_count", 0)) > 0
        ]
        facets.append(Facet(name=name, buckets=buckets))
    return facets


def facets_from_sources(sources: list[dict[str, Any]]) -> list[Facet]:
    """Fallback when BM25 (which carries the aggregations) failed: count over the candidate pool."""
    counts: dict[str, dict[str, int]] = {n: {} for n in FACET_ORDER}
    for src in sources:
        for name, key in (("brand", "brand"), ("colour", "colour"), ("size", "size")):
            value = src.get(key)
            if value:
                counts[name][str(value)] = counts[name].get(str(value), 0) + 1
        path = src.get("category_path")
        if isinstance(path, str) and path:
            parts = path.split(".")
            node = ".".join(parts[:2])
            counts["category"][node] = counts["category"].get(node, 0) + 1
        price = src.get("price")
        if isinstance(price, int | float):
            for lo, hi in PRICE_BUCKETS:
                if (lo is None or price >= lo) and (hi is None or price < hi):
                    label = price_bucket_label(lo, hi)
                    counts["price"][label] = counts["price"].get(label, 0) + 1
    out = []
    for name in FACET_ORDER:
        items = counts[name]
        if name == "price":
            order = [price_bucket_label(lo, hi) for lo, hi in PRICE_BUCKETS]
            buckets = [FacetBucket(value=v, count=items[v]) for v in order if v in items]
        else:
            buckets = [
                FacetBucket(value=v, count=c) for v, c in sorted(items.items(), key=lambda kv: -kv[1])[:20]
            ]
        out.append(Facet(name=name, buckets=buckets))
    return out

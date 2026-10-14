"""OpenSearch request bodies."""

from __future__ import annotations

from souq_search.models import Filters
from souq_search.retrieval import queries as q


def test_filter_clauses() -> None:
    f = Filters(
        price_min=10,
        price_max=200,
        currency="AED",
        brands=["Nike"],
        colours=["Black"],
        sizes=["xl"],
        category="Electronics > Audio",
    )
    clauses = q.filter_clauses(f, apply_currency=False)
    assert {"range": {"price": {"gte": 10, "lte": 200}}} in clauses
    assert {"terms": {"colour": ["black"]}} in clauses
    assert {"term": {"category_levels": "electronics.audio"}} in clauses
    assert not any("currency" in str(c) for c in clauses)
    assert any("currency" in str(c) for c in q.filter_clauses(f, apply_currency=True))


def test_category_slug_mirrors_ltree() -> None:
    assert (
        q.category_slug("Clothing, Shoes & Jewelry > Men > Shoes") == "clothing_shoes_and_jewelry.men.shoes"
    )
    assert q.category_slug("electronics.audio") == "electronics.audio"


def test_bm25_is_term_centric_with_translit() -> None:
    terms = q.make_terms("shanta for laptop", {"shanta": "شنطه"})
    assert terms.terms == ["shanta", "laptop"]  # stopword dropped
    body = q.bm25_body(terms, [], 100, None)
    should = body["query"]["bool"]["must"][0]["bool"]["should"]
    assert len(should) == 2
    assert "شنطه" in str(should[0])
    assert body["query"]["bool"]["must"][0]["bool"]["minimum_should_match"] == q.MIN_SHOULD_MATCH


def test_empty_terms_match_all_with_filters() -> None:
    body = q.bm25_body(q.make_terms(""), [{"term": {"x": 1}}], 5, None)
    assert body["query"]["bool"]["must"] == [{"match_all": {}}]


def test_knn_filters_inside_knn() -> None:
    body = q.knn_body([0.1] * 4, [{"term": {"x": 1}}], 50)
    assert body["query"]["knn"]["embedding"]["filter"] == {"bool": {"filter": [{"term": {"x": 1}}]}}
    assert body["query"]["knn"]["embedding"]["k"] == 50


def test_tail_body_excludes_pool_and_sorts_totally() -> None:
    body = q.tail_body(q.make_terms("shoes"), [], ["A", "B"], 10, "pit", "5m", [1.5, "A"])
    assert body["sort"] == [{"_score": "desc"}, {"sku": "asc"}]
    assert body["search_after"] == [1.5, "A"]
    assert body["pit"] == {"id": "pit", "keep_alive": "5m"}
    assert {"terms": {"sku": ["A", "B"]}} in body["query"]["bool"]["must_not"]


def test_facets_parse_and_fallback() -> None:
    aggs = {
        "brand": {"buckets": [{"key": "Nike", "doc_count": 3}]},
        "price": {"buckets": [{"key": "0-50", "doc_count": 2}, {"key": "50-100", "doc_count": 0}]},
    }
    facets = q.parse_facets(aggs)
    assert [f.name for f in facets] == ["brand", "price"]
    assert facets[1].buckets[0].value == "0-50"
    assert len(facets[1].buckets) == 1
    fb = q.facets_from_sources([{"brand": "Nike", "price": 75.0, "category_path": "a.b.c"}])
    assert {f.name: [b.value for b in f.buckets] for f in fb}["category"] == ["a.b"]
    assert q.price_bucket_label(1000, None) == "1000+"

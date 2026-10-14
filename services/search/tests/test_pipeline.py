"""Pipeline behaviour against in-memory fakes: stages, degradation, LLM/cache paths, pagination."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeBackend, FakeCache, FakeCrossEncoder, catalog

from souq_common.budgets import load_budgets
from souq_common.embeddings import FakeEmbedder
from souq_search.models import Cursor, Filters, SearchOptions, SearchRequest
from souq_search.pagination import SessionStore
from souq_search.parsing.llm import LlmQueryParser
from souq_search.parsing.llm_fixtures import FixtureStore, LlmTransport
from souq_search.parsing.rules import parse_rules
from souq_search.pipeline import (
    BackendUnavailable,
    EmbedService,
    InvalidCursor,
    PipelineOptions,
    SearchPipeline,
)
from souq_search.rerank import Reranker


def make(
    backend: FakeBackend | None = None,
    model: Any = "default",
    llm: LlmQueryParser | None = None,
    cache: FakeCache | None = None,
) -> SearchPipeline:
    return SearchPipeline(
        backend=backend or FakeBackend(catalog()),
        budgets=load_budgets().search,
        embed=EmbedService(FakeEmbedder()),
        reranker=Reranker(FakeCrossEncoder() if model == "default" else model),
        llm=llm,
        cache=cache,  # type: ignore[arg-type]
        sessions=SessionStore(None, 300),
    )


def llm_parser(tmp_path: Path, answers: dict[str, dict[str, Any]], lang: str = "en") -> LlmQueryParser:
    """A replay-mode parser whose fixtures are written for the given normalised queries."""
    store = FixtureStore(tmp_path)
    parser = LlmQueryParser(LlmTransport("replay", store=store), "claude-haiku-5-5")
    for normalised, answer in answers.items():
        body = parser.build_request(normalised, lang, parse_rules(normalised, lang))
        response = {"content": [{"type": "text", "text": json.dumps(answer)}], "stop_reason": "end_turn"}
        store.save("claude-haiku-5-5", body, response, 420, 60, recorded_at="synthetic")
    return parser


ANSWER = {
    "rewritten_query": "running shoes",
    "price_min": None,
    "price_max": 200,
    "currency": "AED",
    "brands": ["Nike"],
    "colours": [],
    "sizes": [],
}


async def test_first_page_shape_and_rerank() -> None:
    resp = await make().search(SearchRequest(q="nike running shoes", lang="en", limit=6))
    assert len(resp.items) == 6
    assert all(i.brand == "Nike" for i in resp.items)
    assert {i.rank_source for i in resp.items} == {"rerank"}
    assert resp.degraded == []
    assert resp.parsed_query.parser == "rule"
    assert resp.parsed_query.filters.brands == ["Nike"]
    assert resp.applied_filters.brands == ["Nike"]
    assert resp.next_cursor is not None
    assert resp.next_cursor.window_pos == 6
    for stage_name in ("normalise", "parse", "bm25", "embed", "knn", "fuse", "rerank", "total"):
        assert stage_name in resp.timings_ms
    assert {f.name for f in resp.facets} == {"category", "brand", "price", "colour", "size"}


async def test_colour_and_price_filters_are_applied() -> None:
    resp = await make().search(SearchRequest(q="black backpack under 200 AED", lang="en", limit=50))
    assert resp.items
    assert all(i.colour == "black" and i.price.amount <= 200 for i in resp.items)
    assert resp.parsed_query.filters.currency == "AED"


async def test_arabic_query_localises_titles() -> None:
    resp = await make().search(SearchRequest(q="حقيبة ظهر سوداء", lang="auto", limit=5))
    assert resp.parsed_query.detected_lang == "ar"
    assert resp.parsed_query.filters.colours == ["black"]
    assert resp.items
    assert all(i.title.startswith("حقيبة") for i in resp.items)


async def test_rerank_over_budget_degrades_to_fused_order() -> None:
    resp = await make(model=FakeCrossEncoder(delay_s=0.5)).search(SearchRequest(q="headphones", lang="en"))
    assert "rerank" in resp.degraded
    assert {i.rank_source for i in resp.items} == {"fused"}


async def test_reranker_busy_degrades_immediately() -> None:
    pipeline = make(model=FakeCrossEncoder(delay_s=0.5))
    await pipeline.search(SearchRequest(q="headphones", lang="en"))  # times out, thread still running
    resp = await pipeline.search(SearchRequest(q="backpack", lang="en"))
    assert "rerank" in resp.degraded
    assert resp.timings_ms["rerank"] < 50  # did not wait for the busy slot


async def test_missing_reranker_model_degrades() -> None:
    resp = await make(model=None).search(SearchRequest(q="headphones", lang="en"))
    assert resp.degraded == ["rerank"]


async def test_rerank_disabled_by_option_is_not_degraded() -> None:
    req = SearchRequest(q="headphones", lang="en", options=SearchOptions(rerank=False))
    resp = await make().search(req)
    assert resp.degraded == []
    assert {i.rank_source for i in resp.items} == {"fused"}


async def test_knn_failure_degrades_but_serves_bm25() -> None:
    backend = FakeBackend(catalog())
    backend.fail.add("knn")
    resp = await make(backend).search(SearchRequest(q="headphones", lang="en"))
    assert resp.degraded == ["knn"]
    assert resp.items


async def test_bm25_timeout_degrades() -> None:
    backend = FakeBackend(catalog())
    backend.delay_s["bm25"] = 0.3
    resp = await make(backend).search(SearchRequest(q="headphones", lang="en"))
    assert "bm25" in resp.degraded
    assert resp.items  # kNN still answered


async def test_both_retrievers_down_is_unavailable() -> None:
    backend = FakeBackend(catalog())
    backend.fail.update({"bm25", "knn"})
    with pytest.raises(BackendUnavailable):
        await make(backend).search(SearchRequest(q="headphones", lang="en"))


async def test_ablation_single_retriever() -> None:
    pipeline = make()
    resp = await pipeline.search(
        SearchRequest(q="headphones", lang="en"),
        PipelineOptions(retrievers=frozenset({"bm25"}), rerank=False),
    )
    assert "knn" not in resp.timings_ms
    assert resp.items


async def test_llm_path_for_constraint_like_query(tmp_path: Path) -> None:
    llm = llm_parser(tmp_path, {"cheap nike running shoes two hundred dirhams": ANSWER})
    cache = FakeCache()
    pipeline = make(llm=llm, cache=cache)
    resp = await pipeline.search(
        SearchRequest(q="cheap Nike running shoes two hundred dirhams", lang="en", limit=50)
    )
    assert resp.parsed_query.parser == "llm"
    assert resp.parsed_query.rewritten == "running shoes"
    assert resp.applied_filters.price_max == 200
    assert all(i.price.amount <= 200 and i.brand == "Nike" for i in resp.items)
    assert resp.degraded == []
    await asyncio.gather(*pipeline._background)  # the cache write is fire-and-forget
    assert cache.store_calls
    assert cache.store_calls[0][2]["filters"]["price_max"] == 200


async def test_llm_fixture_missing_degrades_to_rule_result(tmp_path: Path) -> None:
    llm = llm_parser(tmp_path, {})
    resp = await make(llm=llm).search(SearchRequest(q="cheap nike shoes", lang="en"))
    assert resp.parsed_query.parser == "rule"
    assert "llm_parse" in resp.degraded
    assert resp.parsed_query.filters.brands == ["Nike"]


async def test_semantic_cache_hit_skips_llm(tmp_path: Path) -> None:
    cache = FakeCache()
    cache.entries[("cheap nike shoes", "en")] = {
        "filters": {"price_max": 150, "brands": ["Nike"]},
        "rewritten": "shoes",
    }
    llm = llm_parser(tmp_path, {})  # would degrade if called
    resp = await make(llm=llm, cache=cache).search(SearchRequest(q="cheap nike shoes", lang="en"))
    assert resp.parsed_query.parser == "cache"
    assert resp.applied_filters.price_max == 150
    assert resp.degraded == []


async def test_semantic_cache_down_still_calls_llm(tmp_path: Path) -> None:
    cache = FakeCache()
    cache.fail = True
    llm = llm_parser(tmp_path, {"cheap nike running shoes two hundred dirhams": ANSWER})
    resp = await make(llm=llm, cache=cache).search(
        SearchRequest(q="cheap nike running shoes two hundred dirhams", lang="en")
    )
    assert resp.parsed_query.parser == "llm"
    assert resp.degraded == ["semantic_cache"]


async def test_llm_disabled_by_option(tmp_path: Path) -> None:
    llm = llm_parser(tmp_path, {})
    req = SearchRequest(q="cheap nike shoes", lang="en", options=SearchOptions(llm=False))
    resp = await make(llm=llm).search(req)
    assert resp.parsed_query.parser == "rule"
    assert resp.degraded == []


async def test_zero_results_relax_parsed_filters() -> None:
    # The parsed ceiling is below every price in the fake catalog (min 50 AED).
    resp = await make().search(SearchRequest(q="puma headphones under 40 aed", lang="en", limit=5))
    assert resp.items
    assert resp.parsed_query.filters.price_max == 40
    assert resp.applied_filters.price_max is None


async def test_request_filters_win_and_are_kept_when_relaxing() -> None:
    req = SearchRequest(q="backpack", lang="en", filters=Filters(brands=["Puma"], price_max=150), limit=50)
    resp = await make().search(req)
    assert resp.items
    assert all(i.brand == "Puma" and i.price.amount <= 150 for i in resp.items)


async def _all_pages(pipeline: SearchPipeline, req: SearchRequest, max_pages: int = 20) -> list[list[str]]:
    pages = []
    resp = await pipeline.search(req)
    pages.append([i.sku for i in resp.items])
    while resp.next_cursor is not None and len(pages) < max_pages:
        resp = await pipeline.search(req.model_copy(update={"cursor": resp.next_cursor}))
        pages.append([i.sku for i in resp.items])
    return pages


async def test_pages_never_overlap_or_skip() -> None:
    backend = FakeBackend(catalog())
    pipeline = make(backend)
    req = SearchRequest(q="nike", lang="en", limit=7)
    pages = await _all_pages(pipeline, req)
    flat = [s for p in pages for s in p]
    assert len(flat) == len(set(flat)), "a SKU was served twice"
    nike = {p["sku"] for p in catalog() if p["brand"] == "Nike"}
    assert set(flat) == nike, "pagination skipped results"


async def test_page_two_is_stable_even_if_rerank_would_change(tmp_path: Path) -> None:
    pipeline = make()
    req = SearchRequest(q="headphones", lang="en", limit=10)
    first = await pipeline.search(req)
    assert first.next_cursor is not None
    pipeline.reranker = Reranker(FakeCrossEncoder(delay_s=0.5))  # a fresh rerank would now time out
    second = await pipeline.search(req.model_copy(update={"cursor": first.next_cursor}))
    assert not {i.sku for i in first.items} & {i.sku for i in second.items}
    assert second.items[0].rank_source == "rerank"  # served from the frozen window


async def test_lexical_tail_beyond_the_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    budgets = load_budgets().search
    small = {**budgets.as_dict(), "candidates_per_retriever": 10, "rerank_window": 5}
    from souq_common.budgets import Budgets

    backend = FakeBackend(catalog())
    pipeline = make(backend)
    pipeline.b = Budgets(small)
    req = SearchRequest(q="shoes", lang="en", limit=8, options=SearchOptions(rerank=True))
    pages = await _all_pages(pipeline, req)
    flat = [s for p in pages for s in p]
    assert len(flat) == len(set(flat))
    shoes = {p["sku"] for p in catalog() if "shoes" in p["title_en"]}
    assert shoes <= set(flat) | set()  # every lexical match is reachable
    assert backend.pits_opened == 1  # opened lazily, once, when the tail was first reached
    resp = await pipeline.search(req)
    assert resp.next_cursor is not None
    assert resp.next_cursor.pit_id == ""


async def test_cursor_from_another_query_is_rejected() -> None:
    pipeline = make()
    resp = await pipeline.search(SearchRequest(q="headphones", lang="en", limit=5))
    assert resp.next_cursor is not None
    with pytest.raises(InvalidCursor):
        await pipeline.search(SearchRequest(q="backpack", lang="en", limit=5, cursor=resp.next_cursor))


async def test_expired_session_recomputes_first_page() -> None:
    pipeline = make()
    resp = await pipeline.search(SearchRequest(q="headphones", lang="en", limit=5))
    assert resp.next_cursor is not None
    stale = Cursor(query_key=resp.next_cursor.query_key.split(".")[0] + ".deadbeef", pit_id="", window_pos=5)
    again = await pipeline.search(SearchRequest(q="headphones", lang="en", limit=5, cursor=stale))
    assert again.items


async def test_sparse_fields_become_boosts_not_filters() -> None:
    pipeline = make()
    pipeline.coverage = {"brand": 0.95, "colour": 0.5, "size": 0.16}
    resp = await pipeline.search(
        SearchRequest(
            q="nike black running shoes size 40", lang="en", limit=50, options=SearchOptions(debug=True)
        )
    )
    assert resp.parsed_query.filters.colours == ["black"]
    assert resp.parsed_query.filters.sizes == ["40"]
    assert resp.applied_filters.brands == ["Nike"]  # dense field: hard
    assert resp.applied_filters.colours is None
    assert resp.applied_filters.sizes is None
    assert resp.debug
    assert resp.debug["boosted"] == {"colours": ["black"], "sizes": ["40"]}
    assert any(i.colour != "black" for i in resp.items)  # not excluded, just ranked
    assert resp.items[0].colour == "black"


async def test_request_facets_stay_hard_even_on_sparse_fields() -> None:
    pipeline = make()
    pipeline.coverage = {"colour": 0.5}
    resp = await pipeline.search(
        SearchRequest(q="backpack", lang="en", filters=Filters(colours=["red"]), limit=50)
    )
    assert resp.items
    assert all(i.colour == "red" for i in resp.items)


async def test_rerank_mode_rrf_blends_fused_and_rerank_orders() -> None:
    from souq_common.budgets import Budgets

    pipeline = make()
    replace = await pipeline.search(
        SearchRequest(q="headphones", lang="en", limit=10, options=SearchOptions(debug=True))
    )
    pipeline.b = Budgets({**load_budgets().search.as_dict(), "rerank_mode": "rrf", "rerank_rrf_weight": 1.0})
    blended = await pipeline.search(
        SearchRequest(q="headphones", lang="en", limit=10, options=SearchOptions(debug=True))
    )
    assert {i.rank_source for i in blended.items} == {"rerank"}
    assert blended.debug is not None
    assert replace.debug is not None
    window = [s for s, _ in blended.debug["window"]]
    assert sorted(window) == sorted(s for s, _ in replace.debug["window"])  # same candidates, new order
    assert all(0 < sc < 1 for _, sc in blended.debug["window"])  # RRF scores, not cross-encoder logits

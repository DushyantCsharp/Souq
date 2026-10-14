"""Deep paging stays consistent while the index is being written to.

The query matches a closed set of ``N`` products (unique token + unique brand passed as an explicit filter,
which the search service applies to BM25 *and* inside kNN), so the expected union of all pages is known
exactly. ``N`` exceeds the fused candidate pool (``candidates_per_retriever`` per retriever), so the later
pages run past the frozen pool into the lexical tail, which pages an OpenSearch point-in-time with
``search_after``.

Between every page the test rewrites half of the set so their BM25 scores move (the token's term
frequency flips between 1 and 3), inserts unrelated products, and waits until those writes are searchable.
Without a stable snapshot, moving scores shift ``search_after`` positions and pages overlap or skip.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import pytest
from souq_testing.db import product_versions, upsert_products
from souq_testing.factories import Product, catalog
from souq_testing.services import Stack
from souq_testing.streams import os_count, os_refresh, wait_for, wait_for_doc

pytestmark = [pytest.mark.integration, pytest.mark.slow]

N = 230
LIMIT = 50
PAGES = 5  # ceil(N / LIMIT)


def with_reps(p: Product, token: str, reps: int) -> Product:
    base = p.title_en or ""
    return p.with_(title_en=f"{' '.join([token] * reps)} {base}", title_ar=f"{token} {p.title_ar}")


def test_five_pages_no_duplicates_no_gaps_under_writes(stack: Stack, token: str, sku_prefix: str) -> None:
    brand = f"pg{token}"
    bases = {p.sku: p.with_(brand=brand) for p in catalog(N, prefix=sku_prefix)}
    reps = {sku: 1 + n % 3 for n, sku in enumerate(sorted(bases))}
    originals = [with_reps(bases[sku], token, reps[sku]) for sku in sorted(bases)]
    expected = set(bases)
    for start in range(0, N, 100):
        upsert_products(stack.database_url, originals[start : start + 100])

    brand_query = {"term": {"brand.kw": brand}}
    wait_for(
        lambda: os_count(stack.opensearch_url, "products_read", brand_query) == N, 120, f"{N} docs indexed"
    )
    os_refresh(stack.opensearch_url, "products_read")

    request: dict[str, Any] = {
        "lang": "en",
        "limit": LIMIT,
        "filters": {"brands": [brand]},
        "options": {"llm": False, "rerank": False},
    }
    pages: list[list[dict[str, Any]]] = []
    noise_start = 10_000
    cursor: dict[str, Any] | None = None
    writes = 0

    for page_no in range(1, PAGES + 2):
        body = stack.search_ok(token, **request, **({"cursor": cursor} if cursor else {}))
        pages.append(body["items"])
        cursor = body.get("next_cursor")
        if cursor is None:
            break

        # Write between pages: flip the term frequency of every other product of the set (changes BM25
        # order), add unrelated products, and wait until it is all searchable on the live index.
        flipped = []
        for i, sku in enumerate(sorted(bases)):
            if (i + page_no) % 2 == 0:
                reps[sku] = 1 if reps[sku] >= 2 else 3
                flipped.append(
                    with_reps(bases[sku], token, reps[sku]).with_(price=bases[sku].price + page_no)
                )
        noise = catalog(20, start=noise_start, prefix=sku_prefix + "N")
        noise_start += 20
        upsert_products(
            stack.database_url, flipped + [n.with_(title_en=f"{token} {n.title_en}") for n in noise]
        )
        versions = product_versions(stack.database_url, [flipped[-1].sku])
        wait_for_doc(
            stack.opensearch_url, "products_read", flipped[-1].sku, 90, min_version=versions[flipped[-1].sku]
        )
        wait_for_doc(stack.opensearch_url, "products_read", noise[-1].sku, 90)
        os_refresh(stack.opensearch_url, "products_read")
        writes += 1

    seen = [item["sku"] for page in pages for item in page]
    sources = Counter(item["rank_source"] for page in pages for item in page)
    detail = f"pages={[len(p) for p in pages]} sources={dict(sources)}"

    duplicates = sorted(sku for sku, c in Counter(seen).items() if c > 1)
    assert duplicates == [], f"duplicates across pages: {duplicates[:10]} ({detail})"
    gaps = sorted(expected - set(seen))
    assert gaps == [], f"{len(gaps)} products never returned: {gaps[:10]} ({detail})"
    foreign = sorted(set(seen) - expected)
    assert foreign == [], f"products outside the filter/snapshot: {foreign[:10]} ({detail})"

    assert len(pages) == PAGES, detail
    assert all(len(p) == LIMIT for p in pages[:-1]), detail
    assert writes == PAGES - 1
    # Vacuity guards: the PIT-backed tail really served part of the walk, and the index really changed.
    assert sources["lexical_tail"] > 0, f"never reached the PIT tail; nothing was tested ({detail})"
    assert os_count(stack.opensearch_url, "products_read", {"term": {"brand.kw": brand}}) == N

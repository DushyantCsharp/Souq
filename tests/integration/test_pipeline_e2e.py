"""Catalog write → outbox → relay → Redpanda → indexer → OpenSearch → search service, end to end.

Two writers: direct SQL in the feeds-importer shape (products + product_texts + outbox in one transaction)
and the real feeds importer CLI on a CSV with a bad row mixed in.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from souq_testing.containers import opensearch_request
from souq_testing.db import product_versions, unpublished_outbox_count, upsert_products
from souq_testing.factories import bad_feed_rows, feed_row, tagged_catalog, to_csv
from souq_testing.paths import FEEDS_FIXTURES_DIR
from souq_testing.services import Stack
from souq_testing.streams import os_get_doc, wait_for, wait_for_doc

from souq_common.embeddings import FakeEmbedder, document_text

pytestmark = pytest.mark.integration

INDEX_WAIT_S = 90


def found_skus(stack: Stack, q: str, **body: object) -> set[str]:
    resp = stack.search_ok(q, limit=50, options={"llm": False, "rerank": False}, **body)
    return {item["sku"] for item in resp["items"]}


def assert_embedded(stack: Stack, sku: str, source: dict[str, Any]) -> None:
    """The template keeps ``embedding`` out of ``_source``, so prove the vector is indexed by kNN: the
    document's own text, embedded the way the indexer does it, must find exactly that document."""
    vector = FakeEmbedder().encode_documents([document_text(source)])[0]
    # wait_for_doc uses a realtime GET, which sees the document before a refresh; _search does not.
    opensearch_request(stack.opensearch_url, "POST", "/products_read/_refresh", None)
    body = {
        "size": 1,
        "_source": False,
        "query": {
            "knn": {"embedding": {"vector": vector.tolist(), "k": 1, "filter": {"term": {"sku": sku}}}}
        },
    }
    hits = opensearch_request(stack.opensearch_url, "POST", "/products_read/_search", body)["hits"]["hits"]
    assert [h["_id"] for h in hits] == [sku], f"{sku} has no indexed embedding"
    # cosinesimil scores (1 + cos) / 2; the indexer rounds components to 5 decimals.
    assert hits[0]["_score"] == pytest.approx(1.0, abs=1e-3)


def run_feed_cli(stack: Stack, path: Path, fmt: str) -> dict[str, Any]:
    """The real importer entry point (souq-feeds-import), as an operator would run it."""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from souq_feeds.cli import main; sys.exit(main(sys.argv[1:]))",
            str(path),
            "--format",
            fmt,
        ],
        env={**os.environ, **stack.env},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, f"feeds import failed:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])  # type: ignore[no-any-return]


def test_outbox_rows_become_searchable(stack: Stack, token: str, sku_prefix: str) -> None:
    products = tagged_catalog(6, token, prefix=sku_prefix)
    skus = {p.sku for p in products}
    upsert_products(stack.database_url, products)

    wait_for(lambda: unpublished_outbox_count(stack.database_url) == 0, 30, "relay to drain the outbox")
    versions = product_versions(stack.database_url, skus)
    for p in products:
        doc = wait_for_doc(stack.opensearch_url, "products_read", p.sku, INDEX_WAIT_S)
        src = doc["_source"]
        # External versioning: the index version *is* products.version.
        assert doc["_version"] == versions[p.sku]
        assert src["version"] == versions[p.sku]
        assert src["title_en"] == p.title_en
        assert src["title_ar"] == p.title_ar
        assert src["brand"] == p.brand
        assert src["colour"] == p.colour
        assert_embedded(stack, p.sku, src)

    wait_for(lambda: skus <= found_skus(stack, token, lang="en"), 30, f"search for {token} to return {skus}")


def test_titles_are_localized_by_lang(stack: Stack, token: str, sku_prefix: str) -> None:
    [p] = tagged_catalog(1, token, prefix=sku_prefix)
    upsert_products(stack.database_url, [p])
    wait_for_doc(stack.opensearch_url, "products_read", p.sku, INDEX_WAIT_S)
    wait_for(lambda: p.sku in found_skus(stack, token, lang="en"), 30, "searchable")

    def title(lang: str) -> str:
        items = stack.search_ok(token, lang=lang, options={"llm": False, "rerank": False})["items"]
        [item] = [i for i in items if i["sku"] == p.sku]
        return str(item["title"])

    assert title("en") == p.title_en
    assert title("ar") == p.title_ar


def test_update_reindexes_with_a_higher_version(stack: Stack, token: str, sku_prefix: str) -> None:
    [p] = tagged_catalog(1, token, prefix=sku_prefix)
    upsert_products(stack.database_url, [p])
    v1 = wait_for_doc(stack.opensearch_url, "products_read", p.sku, INDEX_WAIT_S)["_version"]

    upsert_products(stack.database_url, [p.with_(price=p.price + 10)])
    doc = wait_for_doc(stack.opensearch_url, "products_read", p.sku, INDEX_WAIT_S, min_version=v1 + 1)
    assert float(doc["_source"]["price"]) == pytest.approx(float(p.price + 10))

    # A no-op upsert does not bump products.version, so it must not produce a newer document either.
    upsert_products(stack.database_url, [p.with_(price=p.price + 10)])
    wait_for(lambda: unpublished_outbox_count(stack.database_url) == 0, 30, "relay to drain the outbox")
    assert product_versions(stack.database_url, [p.sku])[p.sku] == v1 + 1
    current = os_get_doc(stack.opensearch_url, "products_read", p.sku)
    assert current is not None
    assert current["_version"] == v1 + 1


def test_feed_import_cli_to_search(stack: Stack, token: str, sku_prefix: str, tmp_path: Path) -> None:
    products = tagged_catalog(5, token, prefix=sku_prefix)
    code, bad = next((c, r) for c, r in bad_feed_rows() if c == "invalid_price")
    feed = tmp_path / "feed.csv"
    feed.write_text(to_csv([*(feed_row(p) for p in products), bad]), encoding="utf-8")

    summary = run_feed_cli(stack, feed, "csv")
    assert summary["status"] == "completed"
    assert summary["rows_total"] == 6
    assert summary["rows_ok"] == 5
    assert summary["rows_rejected"] == 1
    assert summary["error_counts"] == {code: 1}

    skus = {p.sku for p in products}
    for sku in skus:
        wait_for_doc(stack.opensearch_url, "products_read", sku, INDEX_WAIT_S)
    wait_for(lambda: skus <= found_skus(stack, token, lang="en"), 30, "imported products searchable")
    # The Arabic titles of the same rows carry the token too.
    assert skus <= found_skus(stack, token, lang="ar")


def test_feed_reject_codes_match_fixture(stack: Stack) -> None:
    """Every contract reject code, one row each, from tests/fixtures/feeds/bad_rows.csv."""
    import psycopg

    expected = json.loads((FEEDS_FIXTURES_DIR / "bad_rows.expected.json").read_text(encoding="utf-8"))
    summary = run_feed_cli(stack, FEEDS_FIXTURES_DIR / "bad_rows.csv", "csv")

    assert summary["status"] == "completed"
    assert summary["rows_total"] == expected["rows_total"]
    assert summary["rows_rejected"] == len(expected["rejects"])
    assert summary["rows_ok"] == expected["rows_total"] - len(expected["rejects"])
    counts: dict[str, int] = {}
    for reject in expected["rejects"]:
        counts[reject["code"]] = counts.get(reject["code"], 0) + 1
    assert summary["error_counts"] == counts

    with psycopg.connect(stack.database_url) as conn:
        rows = conn.execute(
            "SELECT row_number, code FROM feed_row_errors WHERE feed_id = %s ORDER BY row_number",
            (summary["id"],),
        ).fetchall()
    assert [(r[0], r[1]) for r in rows] == [(e["row_number"], e["code"]) for e in expected["rejects"]]

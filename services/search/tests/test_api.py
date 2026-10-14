"""HTTP surface: contract shapes and RFC 9457 problem+json errors."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fakes import FakeBackend, FakeCrossEncoder, catalog
from fastapi.testclient import TestClient

from souq_common.budgets import load_budgets
from souq_common.embeddings import FakeEmbedder
from souq_search.app import Components, create_app
from souq_search.pagination import SessionStore
from souq_search.pipeline import EmbedService, SearchPipeline
from souq_search.rerank import Reranker


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend(catalog())


@pytest.fixture
def client(backend: FakeBackend) -> Iterator[TestClient]:
    pipeline = SearchPipeline(
        backend=backend,
        budgets=load_budgets().search,
        embed=EmbedService(FakeEmbedder()),
        reranker=Reranker(FakeCrossEncoder()),
        llm=None,
        cache=None,
        sessions=SessionStore(None, 300),
    )
    with TestClient(create_app(Components(pipeline=pipeline, checks={"reranker": "fake"}))) as c:
        yield c


def test_search_contract_shape(client: TestClient) -> None:
    resp = client.post("/search", json={"q": "nike shoes", "lang": "en", "limit": 3})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) >= {
        "items",
        "facets",
        "applied_filters",
        "parsed_query",
        "degraded",
        "total_estimate",
        "timings_ms",
        "next_cursor",
    }
    item = body["items"][0]
    assert set(item) >= {"sku", "title", "brand", "price", "category_path", "score", "rank_source"}
    assert set(item["price"]) == {"amount", "currency"}
    assert body["parsed_query"]["parser"] == "rule"
    assert body["applied_filters"] == {"brands": ["Nike"]}
    cursor = body["next_cursor"]
    assert set(cursor) >= {"query_key", "pit_id", "window_pos"}
    page2 = client.post("/search", json={"q": "nike shoes", "lang": "en", "limit": 3, "cursor": cursor})
    assert page2.status_code == 200
    assert not {i["sku"] for i in body["items"]} & {i["sku"] for i in page2.json()["items"]}


def test_validation_error_is_problem_json(client: TestClient) -> None:
    resp = client.post("/search", json={"q": "", "limit": 500})
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["type"] == "https://souq.dev/problems/validation-error"


def test_foreign_cursor_is_400(client: TestClient) -> None:
    first = client.post("/search", json={"q": "nike shoes", "lang": "en", "limit": 3}).json()
    resp = client.post("/search", json={"q": "headphones", "lang": "en", "cursor": first["next_cursor"]})
    assert resp.status_code == 400
    assert resp.json()["type"] == "https://souq.dev/problems/invalid-cursor"


def test_backend_down_is_503(client: TestClient, backend: FakeBackend) -> None:
    backend.fail.update({"bm25", "knn"})
    resp = client.post("/search", json={"q": "headphones"})
    assert resp.status_code == 503
    assert resp.json()["type"] == "https://souq.dev/problems/search-unavailable"


def test_health_endpoints(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok", "checks": {}}
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["checks"]["reranker"] == "fake"


def test_openapi_is_exported_and_current() -> None:
    import subprocess
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "export_openapi.py"
    result = subprocess.run(
        [sys.executable, str(script), "--check"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr

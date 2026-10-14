"""The generated SDK speaks the public contract: deepObject filters, API-key header, multipart upload."""

from __future__ import annotations

import io
import json
from urllib.parse import parse_qsl

import httpx
import respx

from souq_admin_sdk import AuthenticatedClient, Client
from souq_admin_sdk.api.feeds import get_feed, upload_feed
from souq_admin_sdk.api.search import search
from souq_admin_sdk.models import Filters, LangParam, Problem, SearchResponse
from souq_admin_sdk.models.feed_format import FeedFormat
from souq_admin_sdk.models.feed_upload import FeedUpload
from souq_admin_sdk.types import File

BASE = "http://gateway.test"


def admin_client(key: str = "dev-admin-key") -> AuthenticatedClient:
    return AuthenticatedClient(base_url=BASE, token=key, prefix="", auth_header_name="X-API-Key")


SEARCH_BODY = {
    "items": [
        {
            "sku": "B1",
            "title": "حذاء رياضي",
            "description": None,
            "brand": "Nike",
            "price": {"amount": 199.0, "currency": "AED"},
            "category_path": "Fashion > Shoes",
            "colour": "red",
            "size": None,
            "score": 0.9,
            "rank_source": "rerank",
        }
    ],
    "facets": [{"name": "brand", "buckets": [{"value": "Nike", "count": 1}]}],
    "applied_filters": {"brands": ["Nike"], "price_max": 300},
    "parsed_query": {
        "original": "حذاء احمر",
        "normalised": "حذاء احمر",
        "detected_lang": "ar",
        "script": "ar",
        "rewritten": None,
        "filters": {"colours": ["red"]},
        "parser": "llm",
        "unparsed_tokens": [],
    },
    "degraded": ["rerank"],
    "next_page_token": "abc.def",
    "total_estimate": 1,
}


@respx.mock
def test_search_sends_deep_object_filters_and_parses_response() -> None:
    route = respx.get(f"{BASE}/v1/search").mock(return_value=httpx.Response(200, json=SEARCH_BODY))
    client = Client(base_url=BASE)

    result = search.sync(
        client=client,
        q="حذاء احمر",
        lang=LangParam.AR,
        filters=Filters(brands=["Nike", "Adidas"], price_max=300),
        limit=10,
    )

    assert isinstance(result, SearchResponse)
    assert result.items[0].sku == "B1"
    assert result.degraded == ["rerank"]
    assert result.next_page_token == "abc.def"
    query = parse_qsl(route.calls.last.request.url.query.decode())
    assert ("filters[brands]", "Nike") in query
    assert ("filters[brands]", "Adidas") in query
    assert ("filters[price_max]", "300") in query
    assert ("lang", "ar") in query
    assert ("limit", "10") in query


@respx.mock
def test_search_problem_responses_are_typed() -> None:
    problem = {
        "type": "https://souq.dev/problems/invalid-page-token",
        "title": "Invalid page token",
        "status": 400,
        "detail": "The page token has expired; restart the search.",
    }
    respx.get(f"{BASE}/v1/search").mock(
        return_value=httpx.Response(400, json=problem, headers={"content-type": "application/problem+json"})
    )
    result = search.sync(client=Client(base_url=BASE), q="x", page_token="stale")
    assert isinstance(result, Problem)
    assert result.type_ == problem["type"]


@respx.mock
def test_feed_upload_uses_api_key_and_multipart() -> None:
    route = respx.post(f"{BASE}/v1/feeds").mock(
        return_value=httpx.Response(202, json={"id": "f-1", "status": "queued"})
    )
    client = admin_client()
    body = FeedUpload(
        file=File(payload=io.BytesIO(b"sku,brand\nB1,Nike\n"), file_name="feed.csv", mime_type="text/csv"),
        format_=FeedFormat.CSV,
    )

    accepted = upload_feed.sync(client=client, body=body)

    assert accepted is not None
    assert not isinstance(accepted, Problem)
    assert accepted.id == "f-1"
    request = route.calls.last.request
    assert request.headers["X-API-Key"] == "dev-admin-key"
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert b'name="file"; filename="feed.csv"' in request.content
    assert b"sku,brand\nB1,Nike\n" in request.content


@respx.mock
def test_feed_status_exposes_error_counts() -> None:
    respx.get(f"{BASE}/v1/feeds/f-1").mock(
        return_value=httpx.Response(
            200,
            content=json.dumps(
                {
                    "id": "f-1",
                    "status": "completed",
                    "rows_total": 3,
                    "rows_ok": 1,
                    "rows_rejected": 2,
                    "error_counts": {"missing_field": 1, "invalid_price": 1},
                    "started_at": "2026-10-13T10:00:00Z",
                    "finished_at": None,
                    "failure_reason": None,
                }
            ),
        )
    )
    client = admin_client("k")
    status = get_feed.sync(client=client, id="f-1")
    assert status is not None
    assert not isinstance(status, Problem)
    assert status.error_counts.additional_properties == {"missing_field": 1, "invalid_price": 1}
    assert status.rows_rejected == 2

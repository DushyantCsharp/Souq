"""A cancelled caller must not tear down the in-flight HTTP request (that leaked pooled connections)."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from souq_search.opensearch import OpenSearch


async def test_cancelled_caller_lets_request_finish_and_frees_slot() -> None:
    finished: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        finished.append(request.url.path)
        return httpx.Response(200, json={"hits": {"hits": []}})

    client = httpx.AsyncClient(base_url="http://os", transport=httpx.MockTransport(handler))
    os_ = OpenSearch("http://os", client, timeout_s=1.0, connect_s=1.0)
    for _ in range(5):
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(os_.search("products_read", {"query": {"match_all": {}}}), 0.005)
    assert finished == []  # callers gave up first...
    await asyncio.sleep(0.1)
    assert len(finished) == 5  # ...but every request still ran to completion
    assert not os_._inflight
    assert (await os_.search("products_read", {})) == {"hits": {"hits": []}}
    await os_.aclose()

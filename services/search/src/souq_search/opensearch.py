"""Minimal async OpenSearch REST client over httpx (search, PIT, terms). Timeouts are set per call from
the stage budget; the stage's asyncio.wait_for is the hard limit, httpx's timeout just frees the socket.

Requests are shielded from caller cancellation. A request torn down mid-flight inside httpx leaks its pooled
connection: after a 5-minute 200 RPS overload all 200 connections were stuck and every call failed with
PoolTimeout until the process restarted. The caller may stop waiting, but the request itself always ends by
its own httpx timeout (bounded by the stage budget) and returns its connection to the pool."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from souq_common.budgets import load_budgets


class OpenSearchError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"opensearch {status}: {body[:300]}")
        self.status = status


class OpenSearch:
    def __init__(
        self,
        base_url: str,
        client: httpx.AsyncClient | None = None,
        *,
        timeout_s: float | None = None,
        connect_s: float | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        if timeout_s is None or connect_s is None:
            b = load_budgets().search
            timeout_s = float(b.opensearch_timeout_ms) / 1000 if timeout_s is None else timeout_s
            connect_s = float(b.opensearch_connect_ms) / 1000 if connect_s is None else connect_s
        self.default_timeout_s = timeout_s
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=100),
            timeout=httpx.Timeout(timeout_s, connect=connect_s),
        )
        self._inflight: set[asyncio.Task[httpx.Response]] = set()

    def _reap(self, task: asyncio.Task[httpx.Response]) -> None:
        self._inflight.discard(task)
        if not task.cancelled():
            task.exception()  # an abandoned request's failure is expected; don't log it as unretrieved

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        task = asyncio.ensure_future(self._client.request(method, path, **kwargs))
        self._inflight.add(task)
        task.add_done_callback(self._reap)
        return await asyncio.shield(task)

    async def aclose(self) -> None:
        if self._inflight:
            await asyncio.gather(*self._inflight, return_exceptions=True)
        await self._client.aclose()

    async def _post(self, path: str, body: dict[str, Any], timeout_s: float | None) -> dict[str, Any]:
        resp = await self._send(
            "POST",
            path,
            json=body,
            timeout=httpx.Timeout(timeout_s) if timeout_s else httpx.USE_CLIENT_DEFAULT,
        )
        if resp.status_code >= 400:
            raise OpenSearchError(resp.status_code, resp.text)
        data: dict[str, Any] = resp.json()
        return data

    async def search(
        self, index: str, body: dict[str, Any], timeout_s: float | None = None
    ) -> dict[str, Any]:
        return await self._post(f"/{index}/_search", body, timeout_s)

    async def pit_search(self, body: dict[str, Any], timeout_s: float | None = None) -> dict[str, Any]:
        """Search inside a point in time (the body carries ``pit``; no index in the path)."""
        return await self._post("/_search", body, timeout_s)

    async def open_pit(self, index: str, keep_alive: str, timeout_s: float | None = None) -> str:
        resp = await self._send(
            "POST",
            f"/{index}/_search/point_in_time",
            params={"keep_alive": keep_alive},
            timeout=httpx.Timeout(timeout_s) if timeout_s else httpx.USE_CLIENT_DEFAULT,
        )
        if resp.status_code >= 400:
            raise OpenSearchError(resp.status_code, resp.text)
        return str(resp.json()["pit_id"])

    async def close_pit(self, pit_id: str) -> None:
        await self._send("DELETE", "/_search/point_in_time", json={"pit_id": [pit_id]})

    async def ping(self, timeout_s: float | None = None) -> bool:
        timeout_s = timeout_s or self.default_timeout_s
        try:
            resp = await self._send("GET", "/_cluster/health", timeout=timeout_s)
        except httpx.HTTPError:
            return False
        return resp.status_code == 200 and resp.json().get("status") in ("green", "yellow")

    async def index_exists(self, index: str, timeout_s: float | None = None) -> bool:
        timeout_s = timeout_s or self.default_timeout_s
        try:
            resp = await self._send("HEAD", f"/{index}", timeout=timeout_s)
        except httpx.HTTPError:
            return False
        return resp.status_code == 200

    async def count(
        self, index: str, query: dict[str, Any] | None = None, timeout_s: float | None = None
    ) -> int:
        data = await self._post(f"/{index}/_count", {"query": query or {"match_all": {}}}, timeout_s)
        return int(data["count"])

    async def coverage(
        self, index: str, fields: list[str], timeout_s: float | None = None
    ) -> dict[str, float]:
        """Share of documents that have each field."""
        total = await self.count(index, None, timeout_s)
        if not total:
            return {}
        return {f: await self.count(index, {"exists": {"field": f}}, timeout_s) / total for f in fields}

    async def terms(
        self, index: str, field: str, size: int, timeout_s: float | None = None
    ) -> dict[str, int]:
        body = {"size": 0, "aggs": {"t": {"terms": {"field": field, "size": size}}}}
        data = await self.search(index, body, timeout_s)
        return {str(b["key"]): int(b["doc_count"]) for b in data["aggregations"]["t"]["buckets"]}

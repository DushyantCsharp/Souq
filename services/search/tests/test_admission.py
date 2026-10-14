"""Admission control sheds beyond the in-flight limit with 503 + Retry-After and recovers afterwards."""

from __future__ import annotations

import asyncio

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from souq_search.admission import PROBLEM_TYPE, AdmissionControl


def make_app(release: asyncio.Event) -> AdmissionControl:
    async def search(_: Request) -> JSONResponse:
        await release.wait()
        return JSONResponse({"items": []})

    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    inner = Starlette(routes=[Route("/search", search, methods=["POST"]), Route("/healthz", healthz)])
    return AdmissionControl(inner, limit=2, retry_after_s=3)


async def test_sheds_beyond_limit_then_recovers() -> None:
    release = asyncio.Event()
    app = make_app(release)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        held = [asyncio.create_task(client.post("/search", json={})) for _ in range(2)]
        await asyncio.sleep(0.05)
        assert app.inflight == 2

        shed = await client.post("/search", json={})
        assert shed.status_code == 503
        assert shed.headers["retry-after"] == "3"
        assert shed.headers["content-type"] == "application/problem+json"
        assert shed.json()["type"] == PROBLEM_TYPE

        assert (await client.get("/healthz")).status_code == 200  # only /search is admission-controlled

        release.set()
        assert [r.status_code for r in await asyncio.gather(*held)] == [200, 200]
        assert app.inflight == 0
        assert (await client.post("/search", json={})).status_code == 200


async def test_limit_zero_disables() -> None:
    release = asyncio.Event()
    release.set()
    app = make_app(release)
    app.limit = 0
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        codes = await asyncio.gather(*(client.post("/search", json={}) for _ in range(5)))
    assert {r.status_code for r in codes} == {200}

"""Admission control: a bounded number of in-flight /search requests per worker.

Without it, offered load above capacity queues inside the process until every request misses its deadline:
at 200 RPS on a laptop-class node the service answered almost nothing (99.9 % errors) — congestion collapse.
Beyond the limit a request is refused immediately with 503 + Retry-After, so the admitted ones stay within
budget and clients back off. Size the limit with Little's law: per-worker throughput x target latency.
"""

from __future__ import annotations

import json
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

from souq_common.telemetry import metrics

PROBLEM_TYPE = "https://souq.dev/problems/search-overloaded"


class AdmissionControl:
    def __init__(self, app: ASGIApp, limit: int, retry_after_s: int = 1, path: str = "/search") -> None:
        self.app = app
        self.limit = limit
        self.retry_after_s = retry_after_s
        self.path = path
        self.inflight = 0
        self._body = json.dumps(
            {
                "type": PROBLEM_TYPE,
                "title": "Search overloaded",
                "status": 503,
                "detail": "too many requests in flight; retry after the indicated delay",
            }
        ).encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] != self.path or self.limit <= 0:
            await self.app(scope, receive, send)
            return
        if self.inflight >= self.limit:
            metrics().degraded_total.add(1, {"reason": "shed"})
            await self._shed(send)
            return
        self.inflight += 1
        try:
            await self.app(scope, receive, send)
        finally:
            self.inflight -= 1

    async def _shed(self, send: Send) -> None:
        headers: list[tuple[bytes, bytes]] = [
            (b"content-type", b"application/problem+json"),
            (b"retry-after", str(self.retry_after_s).encode()),
            (b"content-length", str(len(self._body)).encode()),
        ]
        start: dict[str, Any] = {"type": "http.response.start", "status": 503, "headers": headers}
        await send(start)
        await send({"type": "http.response.body", "body": self._body})

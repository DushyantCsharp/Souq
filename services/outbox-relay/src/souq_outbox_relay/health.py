"""Tiny HTTP side-car for liveness/readiness/stats (the worker loop itself is not an HTTP app)."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

log = logging.getLogger(__name__)

Probe = Callable[[], tuple[bool, dict[str, Any]]]


def serve_health(
    port: int, live: Probe, ready: Probe, stats: Callable[[], dict[str, Any]]
) -> ThreadingHTTPServer:
    routes: dict[str, Callable[[], tuple[bool, dict[str, Any]]]] = {
        "/healthz": live,
        "/readyz": ready,
        "/stats": lambda: (True, stats()),
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            probe = routes.get(self.path.split("?", 1)[0])
            if probe is None:
                self._send(404, {"error": "not found"})
                return
            try:
                ok, body = probe()
            except Exception as exc:  # a probe must never kill the server thread
                ok, body = False, {"error": str(exc)}
            self._send(200 if ok else 503, body)

        def _send(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="health", daemon=True).start()
    log.info("health endpoints on :%d", port)
    return server


class HeartbeatFile:
    """Touches ``SOUQ_HEALTH_FILE`` from the work loop so an exec HEALTHCHECK can test its mtime."""

    def __init__(self, path: str | None, min_interval_s: float = 1.0) -> None:
        self._path = path
        self._min_interval_s = min_interval_s
        self._last = 0.0

    def beat(self) -> None:
        if not self._path:
            return
        now = time.monotonic()
        if now - self._last < self._min_interval_s:
            return
        self._last = now
        try:
            with open(self._path, "a"):
                pass
            os.utime(self._path, None)
        except OSError as exc:  # health signalling must never break the worker
            log.debug("heartbeat file %s: %s", self._path, exc)

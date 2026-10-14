"""query_log writer: batched multi-row INSERTs from a background task, never on the request path.

``enqueue`` is a non-blocking ``put_nowait``; when the queue is full (database down for a while) rows are
dropped and counted rather than blocking or growing memory without bound.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("souq.search.query_log")

INSERT = (
    "INSERT INTO query_log (query_hash, query_text, lang, detected_script, parser_path, latency_ms, degraded,"
    " result_count) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)"
)


@dataclass(frozen=True)
class QueryLogRow:
    query_hash: str
    query_text: str | None
    lang: str
    detected_script: str
    parser_path: str
    latency_ms: dict[str, float]
    degraded: list[str]
    result_count: int

    def as_params(self) -> tuple[Any, ...]:
        return (
            self.query_hash,
            self.query_text,
            self.lang,
            self.detected_script,
            self.parser_path,
            json.dumps(self.latency_ms),
            list(self.degraded),
            self.result_count,
        )


class QueryLogWriter:
    def __init__(
        self,
        dsn: str,
        batch_size: int = 200,
        flush_ms: float = 1000,
        max_queue: int = 10_000,
        connect_timeout_s: float = 2.0,
    ) -> None:
        self.dsn = dsn
        self.connect_timeout_s = connect_timeout_s
        self.batch_size = batch_size
        self.flush_s = flush_ms / 1000
        self.queue: asyncio.Queue[QueryLogRow] = asyncio.Queue(maxsize=max_queue)
        self.dropped = 0
        self.written = 0
        self._task: asyncio.Task[None] | None = None
        self._conn: Any = None
        # Rows taken off the queue but not yet written; kept here so shutdown can still flush them.
        self._pending: list[QueryLogRow] = []

    def enqueue(self, row: QueryLogRow) -> None:
        try:
            self.queue.put_nowait(row)
        except asyncio.QueueFull:
            self.dropped += 1

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="query-log-writer")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        rows, self._pending = self._pending + self._drain(), []
        await self._flush(rows)
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.close()

    def _drain(self) -> list[QueryLogRow]:
        rows: list[QueryLogRow] = []
        while not self.queue.empty() and len(rows) < self.batch_size * 10:
            rows.append(self.queue.get_nowait())
        return rows

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            self._pending.append(await self.queue.get())
            deadline = loop.time() + self.flush_s
            while len(self._pending) < self.batch_size:
                timeout = deadline - loop.time()
                if timeout <= 0:
                    break
                try:
                    self._pending.append(await asyncio.wait_for(self.queue.get(), timeout))
                except TimeoutError:
                    break
            rows = list(self._pending)
            await self._flush(rows)
            del self._pending[: len(rows)]

    async def _connection(self) -> Any:
        if self._conn is None or self._conn.closed:
            import psycopg

            self._conn = await psycopg.AsyncConnection.connect(
                self.dsn, autocommit=True, connect_timeout=max(1, round(self.connect_timeout_s))
            )
        return self._conn

    async def _flush(self, rows: list[QueryLogRow]) -> None:
        if not rows:
            return
        try:
            conn = await self._connection()
            async with conn.cursor() as cur:
                await cur.executemany(INSERT, [r.as_params() for r in rows])
            self.written += len(rows)
        except Exception as exc:
            # Analytics only: drop the batch rather than retrying forever and holding memory.
            self.dropped += len(rows)
            log.warning("query_log flush failed (%s rows dropped): %s", len(rows), type(exc).__name__)
            if self._conn is not None:
                with contextlib.suppress(Exception):
                    await self._conn.close()
                self._conn = None

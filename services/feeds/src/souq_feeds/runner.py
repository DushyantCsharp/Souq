"""Background import execution (bounded thread pool; each import owns one DB connection)."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import UUID

from psycopg_pool import ConnectionPool

from souq_feeds.importer import ImportOutcome, run_import

log = logging.getLogger(__name__)


class ImportRunner:
    def __init__(self, pool: ConnectionPool[Any], workers: int = 2, batch_size: int = 1000) -> None:
        self._pool = pool
        self._batch_size = batch_size
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="feed-import")
        self.stop_event = threading.Event()

    def submit(self, feed_id: UUID) -> Future[ImportOutcome | None]:
        return self._executor.submit(self._run, feed_id)

    def _run(self, feed_id: UUID) -> ImportOutcome | None:
        try:
            with self._pool.connection() as conn:
                return run_import(conn, feed_id, self._batch_size, self.stop_event)
        except Exception:
            log.exception("feed %s: import worker failed", feed_id)
            raise

    def recover(self) -> int:
        """Re-run feeds left queued/running by a previous process — only those whose upload is on this
        host (another replica may own the rest; the advisory lock prevents double imports)."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT id, file_path FROM feed_imports"
                " WHERE status IN ('queued', 'running') ORDER BY created_at"
            ).fetchall()
        n = 0
        for feed_id, file_path in rows:
            if file_path and Path(file_path).is_file():
                self.submit(feed_id)
                n += 1
        if n:
            log.info("resuming %d interrupted feed import(s)", n)
        return n

    def shutdown(self) -> None:
        # Imports stop at their next batch boundary and go back to 'queued' for recover().
        self.stop_event.set()
        self._executor.shutdown(wait=True, cancel_futures=True)

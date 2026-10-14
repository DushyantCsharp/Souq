"""Resolve the ``products_write`` alias to concrete indices for dual-write during reindexing.

An alias that spans two indices without ``is_write_index`` cannot be written through, and during a
reindex both the old and the new index must receive every change, so the indexer bulks to each concrete
index explicitly. ``scripts/reindex.py`` adds the new index to the alias before backfilling; the 10 s
refresh bounds how long a change can miss the new index (the backfill covers that window).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)


class NoWriteIndexError(RuntimeError):
    """The write alias does not exist (indices not bootstrapped) — indexing must wait, not guess."""


class WriteAliasResolver:
    def __init__(
        self,
        client: Any,
        alias: str,
        refresh_s: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self.alias = alias
        self._refresh_s = refresh_s
        self._clock = clock
        self._indices: tuple[str, ...] = ()
        self._fetched_at: float | None = None
        self._lock = threading.Lock()

    def _fetch(self) -> tuple[str, ...]:
        # ignore=404 makes a missing alias come back as an error body instead of raising NotFoundError.
        resp = self._client.indices.get_alias(name=self.alias, ignore=404)
        if not isinstance(resp, dict) or resp.get("status") == 404 or "error" in resp:
            return ()
        return tuple(sorted(name for name, body in resp.items() if self.alias in body.get("aliases", {})))

    def refresh(self) -> tuple[str, ...]:
        indices = self._fetch()
        with self._lock:
            if indices != self._indices:
                log.info("write alias %s -> %s", self.alias, list(indices))
            self._indices = indices
            self._fetched_at = self._clock()
        return indices

    def maybe_refresh(self) -> None:
        """Refresh if stale. Errors keep the last known indices: a flapping cluster should not stop writes."""
        with self._lock:
            stale = self._fetched_at is None or self._clock() - self._fetched_at >= self._refresh_s
        if not stale:
            return
        try:
            self.refresh()
        except Exception as exc:  # transport errors are retried at the next refresh tick
            log.warning("alias refresh failed: %s", exc)
            with self._lock:
                self._fetched_at = self._clock()

    def invalidate(self) -> None:
        with self._lock:
            self._fetched_at = None

    def indices(self) -> tuple[str, ...]:
        self.maybe_refresh()
        with self._lock:
            indices = self._indices
        if not indices:
            raise NoWriteIndexError(f"alias {self.alias!r} resolves to no index")
        return indices

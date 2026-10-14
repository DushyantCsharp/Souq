"""Pagination sessions (scheme documented in services/search/README.md).

Page 1 computes the fused candidate pool and the reranked window once and stores them under
``query_key = <query hash>.<nonce>`` in Redis for ``pagination_ttl_s``. Later pages are served from that
frozen order, so a rerank that would come out differently on a second run (or a catalog change between
pages) can never make page 2 overlap or skip page 1. Past the pool, results continue lexically through an
OpenSearch point-in-time with ``search_after`` and the pool's SKUs excluded (``rank_source:
"lexical_tail"``); the PIT is opened lazily the first time a page reaches the tail, because opening one
for every first page would exhaust OpenSearch's open-PIT limit under load.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import asdict, dataclass, field
from typing import Any

from souq_search.models import Filters, SearchOptions

KEY_PREFIX = "souq:pg:"


def query_hash(normalised: str, lang: str, filters: Filters | None, options: SearchOptions) -> str:
    material = json.dumps(
        {
            "q": normalised,
            "lang": lang,
            "f": (filters or Filters()).canonical(),
            "llm": options.llm,
            "rerank": options.rerank,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def new_query_key(qhash: str) -> str:
    return f"{qhash}.{secrets.token_hex(4)}"


def query_key_matches(query_key: str, qhash: str) -> bool:
    return query_key.split(".", 1)[0] == qhash


@dataclass
class PageSession:
    window: list[str]
    window_scores: list[float]
    window_source: str  # "rerank" | "fused"
    fused: list[str]
    fused_scores: list[float]
    has_tail: bool
    total_estimate: int
    display_lang: str
    terms: list[str]
    translit: dict[str, str]
    applied_filters: dict[str, Any]
    apply_currency: bool
    facets: list[dict[str, Any]]
    parsed_query: dict[str, Any]
    degraded: list[str]
    pit_id: str | None = None
    boosts: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def ordered(self) -> list[tuple[str, float, str]]:
        head = [(s, sc, self.window_source) for s, sc in zip(self.window, self.window_scores, strict=True)]
        return head + [(s, sc, "fused") for s, sc in zip(self.fused, self.fused_scores, strict=True)]

    def pool(self) -> list[str]:
        return self.window + self.fused


class SessionStore:
    def __init__(self, redis: Any | None, ttl_s: int) -> None:
        self.redis = redis
        self.ttl_s = ttl_s
        # Used when Redis is not configured (unit tests, eval runs): same semantics, process-local.
        self._local: dict[str, str] = {}

    async def save(self, query_key: str, session: PageSession) -> None:
        payload = json.dumps(asdict(session), ensure_ascii=False, separators=(",", ":"))
        if self.redis is None:
            self._local[query_key] = payload
            return
        await self.redis.set(KEY_PREFIX + query_key, payload, ex=self.ttl_s)

    async def load(self, query_key: str) -> PageSession | None:
        if self.redis is None:
            raw: Any = self._local.get(query_key)
        else:
            raw = await self.redis.get(KEY_PREFIX + query_key)
            if raw is not None:
                # sliding expiry, like the PIT keep_alive
                await self.redis.expire(KEY_PREFIX + query_key, self.ttl_s)
        if raw is None:
            return None
        return PageSession(**json.loads(raw))

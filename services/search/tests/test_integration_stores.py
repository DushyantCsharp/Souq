"""Semantic cache (Redis Stack) and query_log (Postgres) against the compose stack."""

from __future__ import annotations

import asyncio
import os
import uuid

import numpy as np
import pytest

from souq_search.parsing.semantic_cache import SemanticCache, number_signature
from souq_search.query_log import QueryLogRow, QueryLogWriter

pytestmark = pytest.mark.integration


def test_number_signature() -> None:
    assert number_signature("shoes under 200") == "n200"
    assert number_signature("shoes 1.5 l 200") == "n1p5_200"
    assert number_signature("shoes") == "none"


async def test_semantic_cache_roundtrip_and_number_guard() -> None:
    import redis.asyncio as aioredis

    r = aioredis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    try:
        await r.ping()
    except Exception:
        pytest.skip("redis not reachable")
    cache = SemanticCache(r, namespace=f"test_{uuid.uuid4().hex[:8]}", dim=8, threshold=0.95, ttl_s=60)
    v = np.ones(8, dtype=np.float32) / np.sqrt(8)
    await cache.store(v, "cheap shoes under 200", "en", {"filters": {"price_max": 200}})
    hit = await cache.lookup(v, "cheap shoes under 200", "en")
    assert hit is not None
    assert hit["filters"]["price_max"] == 200
    assert await cache.lookup(v, "cheap shoes under 300", "en") is None  # same vector, different number
    near = v + np.float32(0.01) * np.arange(8, dtype=np.float32)
    assert await cache.lookup(near / np.linalg.norm(near), "cheap shoes under 200", "en") is not None
    far = np.zeros(8, dtype=np.float32)
    far[0] = 1.0
    assert await cache.lookup(far, "cheap shoes under 200", "en") is None
    assert await cache.lookup(v, "cheap shoes under 200", "ar") is None
    await r.aclose()


async def test_query_log_batches_rows() -> None:
    import psycopg

    dsn = os.environ.get("DATABASE_URL", "postgres://souq:souq@localhost:55432/souq")
    try:
        conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True, connect_timeout=10)
    except Exception:
        pytest.skip("postgres not reachable")
    async with conn:
        exists = await (await conn.execute("SELECT to_regclass('public.query_log')")).fetchone()
        if not exists or exists[0] is None:
            pytest.skip("query_log table not migrated")
        marker = f"it-{uuid.uuid4().hex[:12]}"
        writer = QueryLogWriter(dsn, batch_size=5, flush_ms=50)
        writer.start()
        for i in range(7):
            writer.enqueue(QueryLogRow(marker, f"q{i}", "en", "en", "rule", {"total": 1.0}, ["rerank"], i))
        for _ in range(100):  # connecting can take a while on a loaded machine
            if writer.written >= 7:
                break
            await asyncio.sleep(0.1)
        await writer.stop()
        count = await (
            await conn.execute("SELECT count(*) FROM query_log WHERE query_hash = %s", (marker,))
        ).fetchone()
        assert count is not None
        assert count[0] == 7
        await conn.execute("DELETE FROM query_log WHERE query_hash = %s", (marker,))

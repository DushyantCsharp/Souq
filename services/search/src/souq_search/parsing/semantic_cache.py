"""Semantic cache for LLM parses: Redis Stack (RediSearch) HNSW vector index over normalised-query
embeddings; a hit needs cosine similarity >= ``budgets.search.semantic_cache_similarity`` (0.95).

Embeddings of "shoes under 200" and "shoes under 300" are nearly identical, so a pure vector match would
serve the wrong price. Every entry is therefore tagged with the query's *number signature* (its numeric
tokens) and the KNN search is pre-filtered on an exact signature + language + prompt-version match.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

import numpy as np

INDEX = "souq_llm_cache"
PREFIX = "souq:llmc:"
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_TAG_SAFE = re.compile(r"[^a-z0-9_]")


def _tag(value: str) -> str:
    return _TAG_SAFE.sub("_", value.lower()) or "none"


def number_signature(normalised: str) -> str:
    nums = sorted(_NUMBER.findall(normalised))
    return _tag("n" + "_".join(n.replace(".", "p") for n in nums)) if nums else "none"


def _text(v: Any) -> Any:
    return v.decode() if isinstance(v, bytes) else v


def _first_result(raw: Any) -> dict[str, Any] | None:
    """First hit's fields from an FT.SEARCH reply, RESP2 (flat list) or RESP3 (map) shape."""
    if isinstance(raw, dict):
        results = raw.get(b"results") or raw.get("results") or []
        if not results:
            return None
        first = results[0]
        attrs = first.get(b"extra_attributes") or first.get("extra_attributes") or {}
        return {_text(k): v for k, v in attrs.items()}
    if not raw or int(raw[0]) == 0 or len(raw) < 3:
        return None
    fields = raw[2]
    return {_text(fields[i]): fields[i + 1] for i in range(0, len(fields) - 1, 2)}


class SemanticCache:
    def __init__(
        self,
        redis: Any,
        *,
        namespace: str,
        dim: int,
        threshold: float,
        ttl_s: int,
    ) -> None:
        self.redis = redis
        self.namespace = _tag(namespace)
        self.dim = dim
        self.threshold = threshold
        self.ttl_s = ttl_s
        self._index_ready = False

    async def ensure_index(self) -> None:
        if self._index_ready:
            return
        try:
            await self.redis.execute_command("FT.INFO", INDEX)
        except Exception as exc:
            if "unknown index" not in str(exc).lower() and "no such index" not in str(exc).lower():
                raise
            await self.redis.execute_command(
                "FT.CREATE",
                INDEX,
                "ON",
                "HASH",
                "PREFIX",
                "1",
                PREFIX,
                "SCHEMA",
                "ns",
                "TAG",
                "lang",
                "TAG",
                "sig",
                "TAG",
                "emb",
                "VECTOR",
                "HNSW",
                "6",
                "TYPE",
                "FLOAT32",
                "DIM",
                str(self.dim),
                "DISTANCE_METRIC",
                "COSINE",
            )
        self._index_ready = True

    def _key(self, normalised: str, lang: str) -> str:
        digest = hashlib.sha256(f"{self.namespace}|{lang}|{normalised}".encode()).hexdigest()[:24]
        return PREFIX + digest

    async def lookup(self, vector: np.ndarray, normalised: str, lang: str) -> dict[str, Any] | None:
        await self.ensure_index()
        sig = number_signature(normalised)
        query = f"(@ns:{{{self.namespace}}} @lang:{{{_tag(lang)}}} @sig:{{{sig}}})=>[KNN 1 @emb $vec AS dist]"
        raw = await self.redis.execute_command(
            "FT.SEARCH",
            INDEX,
            query,
            "PARAMS",
            "2",
            "vec",
            np.asarray(vector, dtype=np.float32).tobytes(),
            "SORTBY",
            "dist",
            "RETURN",
            "2",
            "payload",
            "dist",
            "DIALECT",
            "2",
            "LIMIT",
            "0",
            "1",
        )
        values = _first_result(raw)
        if values is None:
            return None
        dist = float(values.get("dist", 2.0))
        similarity = 1.0 - dist  # RediSearch COSINE distance = 1 - cosine similarity
        if similarity < self.threshold:
            return None
        payload = values.get("payload")
        if payload is None:
            return None
        data: dict[str, Any] = json.loads(payload)
        data["_similarity"] = similarity
        return data

    async def store(self, vector: np.ndarray, normalised: str, lang: str, payload: dict[str, Any]) -> None:
        await self.ensure_index()
        key = self._key(normalised, lang)
        mapping = {
            "ns": self.namespace,
            "lang": _tag(lang),
            "sig": number_signature(normalised),
            "emb": np.asarray(vector, dtype=np.float32).tobytes(),
            "payload": json.dumps(payload, ensure_ascii=False),
            "created": str(int(time.time())),
        }
        pipe = self.redis.pipeline(transaction=False)
        pipe.hset(key, mapping=mapping)
        pipe.expire(key, self.ttl_s)
        await pipe.execute()

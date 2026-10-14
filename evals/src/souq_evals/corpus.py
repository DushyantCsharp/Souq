"""The eval index: judged products of the golden set plus seeded random distractors from the ESCI seed,
indexed with the production products_v2 template and the configured embedder (``souq_common.embeddings``,
the same module the indexer uses). Rebuilt only when the corpus, template or embedder changes."""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pyarrow.parquet as pq

from souq_common.embeddings import Embedder, document_text
from souq_evals.golden import GoldenQuery
from souq_evals.paths import CACHE, ESCI, TEMPLATE

PRODUCT_COLUMNS = [
    "sku",
    "title_en",
    "title_ar",
    "description_en",
    "brand",
    "category_ltree",
    "price",
    "currency",
    "colour",
    "size",
]


@dataclass
class Corpus:
    docs: list[dict[str, Any]]
    fingerprint: str


def load_products(path: Path | None = None) -> dict[str, dict[str, Any]]:
    table = pq.read_table(path or ESCI / "products.parquet", columns=PRODUCT_COLUMNS)
    return {str(r["sku"]): r for r in table.to_pylist()}


def to_doc(p: dict[str, Any]) -> dict[str, Any]:
    """Same shape as product_document(sku) — what the indexer sends to OpenSearch."""
    path = str(p.get("category_ltree") or "general.miscellaneous")
    labels = path.split(".")
    price = p.get("price")
    return {
        "sku": p["sku"],
        "title_en": p.get("title_en"),
        "title_ar": p.get("title_ar"),
        "description_en": (p.get("description_en") or None) and str(p["description_en"])[:2000],
        "description_ar": None,
        "brand": p.get("brand"),
        "category_path": path,
        "category_levels": [".".join(labels[: i + 1]) for i in range(len(labels))],
        "price": float(price) if isinstance(price, int | float | Decimal | str) and price != "" else 0.0,
        "currency": p.get("currency") or "AED",
        "colour": p.get("colour"),
        "size": p.get("size"),
    }


def build_corpus(golden: list[GoldenQuery], distractors: int, seed: int = 7) -> Corpus:
    products = load_products()
    judged = sorted({j.sku for q in golden for j in q.judgments if j.sku in products})
    others = sorted(set(products) - set(judged))
    random.Random(seed).shuffle(others)
    skus = judged + sorted(others[:distractors])
    docs = [to_doc(products[s]) for s in skus]
    material = json.dumps(docs, sort_keys=True, ensure_ascii=False, default=str) + TEMPLATE.read_text()
    return Corpus(docs=docs, fingerprint=hashlib.sha256(material.encode()).hexdigest()[:16])


def embed_corpus(docs: list[dict[str, Any]], embedder: Embedder, embedder_name: str) -> np.ndarray:
    """Document vectors, cached per (embedder, document text) in models/artifacts/cache."""
    CACHE.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE / f"eval-embeddings-{embedder_name}.npz"
    texts = [document_text(d) for d in docs]
    keys = [hashlib.sha256(t.encode()).hexdigest()[:20] for t in texts]
    cached: dict[str, np.ndarray] = {}
    if cache_path.exists():
        data = np.load(cache_path)
        cached = dict(zip(data["keys"].tolist(), data["vectors"], strict=True))
    missing = [i for i, k in enumerate(keys) if k not in cached]
    if missing:
        vecs = embedder.encode_documents([texts[i] for i in missing])
        for i, v in zip(missing, vecs, strict=True):
            cached[keys[i]] = v
        np.savez(cache_path, keys=np.array(list(cached)), vectors=np.stack(list(cached.values())))
    return np.stack([cached[k] for k in keys]).astype(np.float32)


def ensure_index(
    base_url: str, index: str, corpus: Corpus, vectors: np.ndarray, embedder_name: str, rebuild: bool = False
) -> bool:
    """Create and fill ``index`` unless it already holds this exact corpus. Returns True if (re)built."""
    fingerprint = f"{corpus.fingerprint}-{embedder_name}"
    with httpx.Client(base_url=base_url, timeout=120.0) as client:
        resp = client.get(f"/{index}/_mapping")
        if resp.status_code == 200 and not rebuild:
            meta = next(iter(resp.json().values()))["mappings"].get("_meta", {})
            if meta.get("souq_eval_fingerprint") == fingerprint:
                return False
        client.delete(f"/{index}")
        template = json.loads(TEMPLATE.read_text())["template"]
        mappings = dict(template["mappings"])
        mappings["_meta"] = {"souq_eval_fingerprint": fingerprint}
        settings = json.loads(json.dumps(template["settings"]))
        settings.setdefault("index", {})["refresh_interval"] = "-1"
        created = client.put(f"/{index}", json={"settings": settings, "mappings": mappings})
        created.raise_for_status()
        for start in range(0, len(corpus.docs), 500):
            lines = []
            for doc, vec in zip(corpus.docs[start : start + 500], vectors[start : start + 500], strict=True):
                lines.append(json.dumps({"index": {"_index": index, "_id": doc["sku"]}}))
                lines.append(
                    json.dumps({**doc, "embedding": [round(float(x), 6) for x in vec]}, ensure_ascii=False)
                )
            bulk = client.post(
                "/_bulk", content="\n".join(lines) + "\n", headers={"Content-Type": "application/x-ndjson"}
            )
            bulk.raise_for_status()
            if bulk.json().get("errors"):
                first = next(i for i in bulk.json()["items"] if i["index"].get("error"))
                raise RuntimeError(f"bulk indexing failed: {first['index']['error']}")
        client.put(f"/{index}/_settings", json={"index": {"refresh_interval": "1s"}}).raise_for_status()
        client.post(f"/{index}/_refresh").raise_for_status()
        # One segment: HNSW graphs and BM25 statistics are then identical run to run.
        client.post(f"/{index}/_forcemerge", params={"max_num_segments": 1}, timeout=600.0).raise_for_status()
    return True

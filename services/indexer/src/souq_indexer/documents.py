"""Event -> OpenSearch document. Only mapped fields are sent, so new payload keys can never trip a
strict mapping and push healthy events into the DLQ."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np

from souq_common.embeddings import Embedder, document_text
from souq_indexer.events import ChangeEvent

DOC_FIELDS: tuple[str, ...] = (
    "sku",
    "brand",
    "category_path",
    "category_levels",
    "price",
    "currency",
    "colour",
    "size",
    "attributes",
    "title_en",
    "title_ar",
    "description_en",
    "description_ar",
    "updated_at",
    "version",
)

# 5 decimals keeps cosine error < 1e-4 while cutting the JSON size of a 1024-d vector roughly in half.
_EMBED_DECIMALS = 5


def _category_levels(path: str) -> list[str]:
    # Cumulative prefixes ("A", "A > B", "A > B > C") so a category filter is a single term query.
    parts = [p.strip() for p in path.split(">") if p.strip()]
    return [" > ".join(parts[: i + 1]) for i in range(len(parts))]


def build_document(event: ChangeEvent) -> dict[str, Any]:
    product = event.product or {}
    doc = {k: product[k] for k in DOC_FIELDS if k in product and product[k] is not None}
    doc["sku"] = event.sku
    doc["version"] = event.version
    if "category_levels" not in doc and isinstance(doc.get("category_path"), str):
        doc["category_levels"] = _category_levels(doc["category_path"])
    if isinstance(doc.get("colour"), str):
        doc["colour"] = doc["colour"].strip().lower()
    return doc


def attach_embeddings(docs: Sequence[dict[str, Any]], embedder: Embedder, batch: int = 256) -> None:
    """Batch-encode documents in place. A zero vector is omitted: cosinesimil rejects it at index time."""
    texts = [document_text(d) for d in docs]
    for start in range(0, len(texts), batch):
        chunk = texts[start : start + batch]
        vectors = np.asarray(embedder.encode_documents(chunk), dtype=np.float32)
        if vectors.shape != (len(chunk), embedder.dim):
            raise ValueError(
                f"embedder returned shape {vectors.shape}, expected ({len(chunk)}, {embedder.dim})"
            )
        norms = np.linalg.norm(vectors, axis=1)
        rounded = np.round(vectors, _EMBED_DECIMALS)
        for offset, (vec, norm) in enumerate(zip(rounded, norms, strict=True)):
            if norm > 0 and np.isfinite(norm):
                docs[start + offset]["embedding"] = vec.tolist()

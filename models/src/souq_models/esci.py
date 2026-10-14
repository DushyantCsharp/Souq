"""Read the data agent's ESCI seed (data/esci/*.parquet) for benchmarks and evals."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from souq_common.embeddings import repo_root

ESCI_DIR = repo_root() / "data" / "esci"


@dataclass(frozen=True)
class JudgedQuery:
    query_id: int
    query: str
    judgments: dict[str, int]  # sku -> gain (E=3, S=2, C=1, I=0)


def load_products(path: Path | None = None, columns: list[str] | None = None) -> dict[str, dict[str, Any]]:
    table = pq.read_table(path or ESCI_DIR / "products.parquet", columns=columns)
    return {str(r["sku"]): r for r in table.to_pylist()}


def load_judged_queries(min_judged: int = 1) -> list[JudgedQuery]:
    queries = {
        int(r["query_id"]): str(r["query"]) for r in pq.read_table(ESCI_DIR / "queries.parquet").to_pylist()
    }
    judged: dict[int, dict[str, int]] = defaultdict(dict)
    for r in pq.read_table(ESCI_DIR / "judgments.parquet").to_pylist():
        judged[int(r["query_id"])][str(r["sku"])] = int(r["gain"])
    out = [JudgedQuery(qid, queries[qid], judged[qid]) for qid in sorted(judged) if qid in queries]
    return [q for q in out if len(q.judgments) >= min_judged]

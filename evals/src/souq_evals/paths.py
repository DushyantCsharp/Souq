"""Repository paths used by the harness."""

from __future__ import annotations

from souq_common.embeddings import repo_root

ROOT = repo_root()
EVALS = ROOT / "evals"
GOLDEN = EVALS / "golden"
FILTER_CASES = EVALS / "filter_parse.jsonl"
THRESHOLDS = EVALS / "thresholds.yaml"
REPORTS = EVALS / "reports"
LABELLING = EVALS / "labelling"
ESCI = ROOT / "data" / "esci"
TEMPLATE = ROOT / "infra" / "opensearch" / "templates" / "products_v2.json"
# models/artifacts is gitignored; embeddings of the eval corpus are cached there between runs.
CACHE = ROOT / "models" / "artifacts" / "cache"
SLICES = ("en", "ar", "arabizi_mixed")

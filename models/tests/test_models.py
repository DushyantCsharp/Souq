"""Static checks on the model registry and the checksum manifest (no downloads)."""

from __future__ import annotations

import re

import pytest

from souq_common.embeddings import repo_root
from souq_models.metrics import ndcg_at_k
from souq_models.registry import DEFAULT_RERANKER, MODELS, RUNTIME_DIRS


def test_revisions_are_pinned_commits() -> None:
    for spec in MODELS.values():
        assert re.fullmatch(r"[0-9a-f]{40}", spec.revision), spec.key
        assert spec.onnx in spec.files
    assert MODELS[DEFAULT_RERANKER].kind == "reranker"
    assert set(RUNTIME_DIRS) == {"reranker", "embedder"}


def test_checksum_manifest_format() -> None:
    manifest = repo_root() / "models" / "checksums.txt"
    if not manifest.exists():
        pytest.skip("checksums.txt not written yet")
    lines = manifest.read_text().splitlines()
    assert lines
    dirs = set(RUNTIME_DIRS.values())
    for line in lines:
        digest, path = line.split("  ", 1)
        assert re.fullmatch(r"[0-9a-f]{64}", digest)
        assert path.split("/", 1)[0] in dirs
        assert not path.startswith("/")


def test_ndcg() -> None:
    assert ndcg_at_k(["a", "b"], {"a": 3, "b": 0}) == pytest.approx(1.0)
    assert ndcg_at_k(["x"], {}) == 0.0

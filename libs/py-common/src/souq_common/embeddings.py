"""Embeddings shared by the search service, the indexer and scripts/reindex.py.

``SOUQ_EMBEDDER=bge-m3`` serves BAAI/bge-m3 dense vectors (1024-d, CLS pooling, L2-normalised) from the
ONNX artefact produced by ``models/scripts/export_embedder.py`` (dynamic int8 by default; see
models/README.md for the quality check against fp32). ``SOUQ_EMBEDDER=fake`` gives deterministic
token-hash vectors for unit tests and offline work. Query and document text go through the same encoder:
bge-m3 needs no instruction prefix for retrieval.
"""

from __future__ import annotations

import hashlib
import os
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from souq_common.settings import get_settings

DIM = 1024
DEFAULT_EMBEDDER_DIR = "models/artifacts/bge-m3-int8"


class Embedder(Protocol):
    dim: int

    def encode_queries(self, texts: list[str]) -> np.ndarray: ...

    def encode_documents(self, texts: list[str]) -> np.ndarray: ...


class FakeEmbedder:
    """Deterministic, L2-normalised token-hash vectors. Shares tokens → positive cosine."""

    dim = DIM

    def _one(self, text: str) -> np.ndarray:
        vec = np.zeros(self.dim, dtype=np.float32)
        for tok in text.lower().split():
            h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "big")
            vec[h % self.dim] += 1.0 if (h >> 32) & 1 else -1.0
        norm = float(np.linalg.norm(vec))
        return vec / norm if norm else vec

    def encode_queries(self, texts: list[str]) -> np.ndarray:
        return np.stack([self._one(t) for t in texts]) if texts else np.zeros((0, self.dim), np.float32)

    def encode_documents(self, texts: list[str]) -> np.ndarray:
        return self.encode_queries(texts)


def repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "models").is_dir() and (parent / "config").is_dir():
            return parent
    return Path.cwd()


def resolve_model_dir(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else repo_root() / path


def ort_threads() -> int:
    """Intra-op threads per ONNX session. Several requests run concurrently, so don't take every core."""
    return int(os.environ.get("SOUQ_ORT_THREADS", "4"))


class OnnxBgeM3Embedder:
    """bge-m3 dense retrieval embeddings via onnxruntime (CPU)."""

    dim = DIM

    def __init__(
        self,
        model_dir: Path,
        *,
        query_max_length: int = 64,
        doc_max_length: int = 256,
        batch_size: int = 16,
        threads: int | None = None,
    ) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        model_path = model_dir / "model.onnx"
        if not model_path.exists():
            raise FileNotFoundError(
                f"{model_path} missing; build it with models/scripts/export_embedder.py (models/README.md)"
            )
        self._tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads or ort_threads()
        opts.inter_op_num_threads = 1
        # Spinning intra-op threads busy-wait between runs; with several sessions per process (embedder,
        # reranker, N uvicorn workers) they burn whole cores idle. SOUQ_ORT_SPIN=1 for single-caller
        # batch jobs.
        opts.add_session_config_entry("session.intra_op.allow_spinning", os.environ.get("SOUQ_ORT_SPIN", "0"))
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # Variable input shapes (query length, rerank batch) make the CPU arena grow to the largest shape seen
        # and never shrink; under load that OOM-killed uvicorn workers. Allocate per run instead.
        opts.enable_cpu_mem_arena = False
        opts.enable_mem_pattern = False
        self._session = ort.InferenceSession(str(model_path), opts, providers=["CPUExecutionProvider"])
        self._input_names = {i.name for i in self._session.get_inputs()}
        self._output_names = [o.name for o in self._session.get_outputs()]
        self._query_max_length = query_max_length
        self._doc_max_length = doc_max_length
        self._batch_size = batch_size
        # The tokenizer's truncation/padding settings are mutable shared state.
        self._lock = threading.Lock()

    def _tokenize(self, texts: list[str], max_length: int) -> tuple[np.ndarray, np.ndarray]:
        with self._lock:
            self._tokenizer.enable_truncation(max_length=max_length)
            self._tokenizer.enable_padding(pad_id=1, pad_token="<pad>")  # XLM-R pad id
            encs = self._tokenizer.encode_batch(texts)
        ids = np.array([e.ids for e in encs], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
        return ids, mask

    def _run(self, ids: np.ndarray, mask: np.ndarray) -> np.ndarray:
        feeds: dict[str, np.ndarray] = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self._input_names:
            feeds["token_type_ids"] = np.zeros_like(ids)
        outputs = self._session.run(None, {k: v for k, v in feeds.items() if k in self._input_names})
        by_name = dict(zip(self._output_names, outputs, strict=True))
        if "dense_vecs" in by_name:
            vecs = np.asarray(by_name["dense_vecs"], dtype=np.float32)
        elif "sentence_embedding" in by_name:
            vecs = np.asarray(by_name["sentence_embedding"], dtype=np.float32)
        else:
            # bge-m3 dense = [CLS] hidden state
            vecs = np.asarray(outputs[0], dtype=np.float32)[:, 0, :]
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        out: np.ndarray = (vecs / np.maximum(norms, 1e-12)).astype(np.float32)
        return out

    def _encode(self, texts: list[str], max_length: int) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), np.float32)
        # Sort by length so each batch pads to a similar length — large win for document backfills.
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        out = np.zeros((len(texts), self.dim), np.float32)
        for start in range(0, len(order), self._batch_size):
            idx = order[start : start + self._batch_size]
            ids, mask = self._tokenize([texts[i] or " " for i in idx], max_length)
            out[idx] = self._run(ids, mask)
        return out

    def encode_queries(self, texts: list[str]) -> np.ndarray:
        return self._encode(texts, self._query_max_length)

    def encode_documents(self, texts: list[str]) -> np.ndarray:
        return self._encode(texts, self._doc_max_length)


def document_text(doc: dict[str, Any]) -> str:
    """The text embedded for a product. Both languages go into one vector (bge-m3 is cross-lingual), so
    an Arabic query can reach an English-only listing and vice versa."""
    category = doc.get("category_path") or ""
    leaf = ""
    if isinstance(category, str) and category:
        # display form "A > B > Leaf" (feeds) or ltree form "a.b.leaf_name" (product_document)
        if ">" in category:
            leaf = category.split(">")[-1].strip()
        else:
            leaf = category.split(".")[-1].replace("_", " ")
    parts = [doc.get("title_en"), doc.get("title_ar"), doc.get("brand"), leaf, doc.get("colour")]
    return " | ".join(str(p) for p in parts if p)


@lru_cache(maxsize=4)
def _load(kind: str, model_dir: str) -> Embedder:
    if kind == "fake":
        return FakeEmbedder()
    if kind == "bge-m3":
        return OnnxBgeM3Embedder(resolve_model_dir(model_dir))
    raise ValueError(f"unknown SOUQ_EMBEDDER={kind!r}")


def get_embedder() -> Embedder:
    """Process-wide embedder (the ONNX session is ~600 MB, load it once)."""
    model_dir = os.environ.get("SOUQ_EMBEDDER_MODEL", DEFAULT_EMBEDDER_DIR)
    return _load(get_settings().souq_embedder, model_dir)

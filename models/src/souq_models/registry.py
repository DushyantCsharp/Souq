"""Pinned model sources. Revisions are commit SHAs on the Hugging Face hub so exports are reproducible."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from souq_common.embeddings import repo_root

ARTIFACTS = repo_root() / "models" / "artifacts"
SRC_DIR = ARTIFACTS / "src"


@dataclass(frozen=True)
class ModelSpec:
    key: str
    repo: str
    revision: str
    kind: Literal["reranker", "embedder"]
    onnx: str  # fp32 graph inside the repo
    files: tuple[str, ...]  # everything to download (graph, external data, tokenizer, config)
    tokenizer: str = "tokenizer.json"
    num_heads: int = 12
    hidden_size: int = 384
    max_length: int = 128
    upstream: str = ""  # the original (PyTorch) model when the repo holds a third-party ONNX export
    notes: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def src_dir(self) -> Path:
        return SRC_DIR / self.key


MODELS: dict[str, ModelSpec] = {
    "mmarco-mMiniLMv2-L12-H384-v1": ModelSpec(
        key="mmarco-mMiniLMv2-L12-H384-v1",
        repo="cross-encoder/mmarco-mMiniLMv2-L12-H384-v1",
        revision="1427fd652930e4ba29e8149678df786c240d8825",
        kind="reranker",
        onnx="onnx/model.onnx",
        files=(
            "onnx/model.onnx",
            "tokenizer.json",
            "tokenizer_config.json",
            "special_tokens_map.json",
            "config.json",
        ),
        num_heads=12,
        hidden_size=384,
        notes="Multilingual MiniLMv2 (L12, H384) cross-encoder fine-tuned on mMARCO (14 languages).",
    ),
    "bge-reranker-v2-m3": ModelSpec(
        key="bge-reranker-v2-m3",
        repo="onnx-community/bge-reranker-v2-m3-ONNX",
        revision="6f5ff65298512715a1e669753bc754d2bc8f367b",
        kind="reranker",
        onnx="onnx/model.onnx",
        files=(
            "onnx/model.onnx",
            "onnx/model.onnx_data",
            "tokenizer.json",
            "tokenizer_config.json",
            "special_tokens_map.json",
            "config.json",
        ),
        num_heads=16,
        hidden_size=1024,
        upstream="BAAI/bge-reranker-v2-m3",
        notes="XLM-RoBERTa-large (568M) multilingual reranker; fp32 ONNX graph exported by onnx-community.",
    ),
    "bge-m3": ModelSpec(
        key="bge-m3",
        repo="BAAI/bge-m3",
        revision="5617a9f61b028005a4858fdac845db406aefb181",
        kind="embedder",
        onnx="onnx/model.onnx",
        files=(
            "onnx/model.onnx",
            "onnx/model.onnx_data",
            "onnx/Constant_7_attr__value",
            "onnx/tokenizer.json",
            "onnx/config.json",
            "onnx/special_tokens_map.json",
            "onnx/tokenizer_config.json",
        ),
        tokenizer="onnx/tokenizer.json",
        num_heads=16,
        hidden_size=1024,
        max_length=256,
        notes="Official fp32 ONNX graph in the BAAI repo; outputs sentence_embedding (dense, 1024-d).",
    ),
}

DEFAULT_RERANKER = "mmarco-mMiniLMv2-L12-H384-v1"
RUNTIME_DIRS = {"reranker": "reranker-int8", "embedder": "bge-m3-int8"}

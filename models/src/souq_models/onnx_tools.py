"""fp32 ONNX graph -> (transformer-fused) -> dynamic int8 runtime artefact."""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from souq_models.registry import ARTIFACTS, ModelSpec

log = logging.getLogger("souq.models")
TWO_GB = 2 * 1024**3


def _graph_bytes(path: Path) -> int:
    total = path.stat().st_size
    for sibling in path.parent.iterdir():
        if sibling.name.startswith(path.name + "_data") or sibling.suffix == ".onnx_data":
            total += sibling.stat().st_size
    return total


def optimize_fp32(spec: ModelSpec, src: Path, dst: Path) -> bool:
    """Fuse attention / skip-layer-norm / GELU with onnxruntime's own graph optimizer (extended level,
    saved offline) so int8 quantisation sees fused ops. Extended-level fusions are CPU-EP specific, which
    is what we serve on. Returns False when fusion fails; quantisation then runs on ``src``."""
    import onnxruntime as ort

    opts = ort.SessionOptions()
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_EXTENDED
    opts.optimized_model_filepath = str(dst)
    if _graph_bytes(src) > TWO_GB:
        opts.add_session_config_entry(
            "session.optimized_model_external_initializers_file_name", dst.name + "_data"
        )
        opts.add_session_config_entry(
            "session.optimized_model_external_initializers_min_size_in_bytes", "1024"
        )
    try:
        ort.InferenceSession(str(src), opts, providers=["CPUExecutionProvider"])
    except Exception as exc:
        log.warning("graph fusion skipped for %s: %s", spec.key, exc)
        return False
    return dst.exists()


def quantize_int8(src: Path, dst: Path) -> None:
    import onnx
    from onnxruntime.quantization import QuantType, quantize_dynamic

    quantize_dynamic(
        model_input=str(src),
        model_output=str(dst),
        weight_type=QuantType.QInt8,
        per_channel=False,
        # Fused com.microsoft ops have no ONNX shape-inference; their float outputs need a declared type.
        extra_options={"MatMulConstBOnly": True, "DefaultTensorType": onnx.TensorProto.FLOAT},
        use_external_data_format=False,
    )


def export(spec: ModelSpec, out_name: str | None = None, fuse: bool = True) -> Path:
    """Build ``models/artifacts/<out_name>/`` = {model.onnx (int8), tokenizer.json, souq_model.json}."""
    src = spec.src_dir / spec.onnx
    out_dir = ARTIFACTS / (out_name or f"{spec.key}-int8")
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ARTIFACTS) as tmp:
        quant_input = src
        fused = False
        if fuse:
            fused_path = Path(tmp) / "fused.onnx"
            fused = optimize_fp32(spec, src, fused_path)
            if fused:
                quant_input = fused_path
        quantize_int8(quant_input, out_dir / "model.onnx")
    shutil.copyfile(spec.src_dir / spec.tokenizer, out_dir / "tokenizer.json")
    tok_cfg = spec.src_dir / Path(spec.tokenizer).with_name("tokenizer_config.json")
    pad_token = "<pad>"
    if tok_cfg.exists():
        pad_token = str(json.loads(tok_cfg.read_text()).get("pad_token", pad_token))
    meta: dict[str, Any] = {
        "name": spec.key,
        "repo": spec.repo,
        "revision": spec.revision,
        "upstream": spec.upstream or spec.repo,
        "kind": spec.kind,
        "max_length": spec.max_length,
        "pad_token": pad_token,
        "pad_id": 1,  # XLM-R / mMiniLMv2 vocab
        "quantization": "onnxruntime dynamic int8 (QInt8 weights, per-tensor, MatMulConstBOnly)",
        "transformer_fusion": fused,
        "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (out_dir / "souq_model.json").write_text(json.dumps(meta, indent=2) + "\n")
    return out_dir


def install(src_dir: Path, runtime_name: str) -> Path:
    """Copy an exported dir to the runtime name the services load (reranker-int8 / bge-m3-int8)."""
    dst = ARTIFACTS / runtime_name
    if dst.resolve() == src_dir.resolve():
        return dst
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src_dir, dst)
    return dst

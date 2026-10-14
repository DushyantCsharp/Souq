"""CPU latency per batch size and rerank quality for the reranker candidates.

    uv run --package souq-models python models/scripts/bench_rerankers.py [--variants ...] [--threads 4]

Latency: (query, product) pairs built exactly like the service builds them (souq_search.pipeline.rerank_text:
localised title | other-language title | brand), truncated at the model's max_length, batch sizes 1/8/16/30,
``--runs`` timed runs after warm-up; p50/p95 wall time of OnnxCrossEncoder.score (tokenisation included).

Quality: nDCG@10 when reranking each ESCI query's judged products (up to 30, in a fixed shuffled order),
over ``--quality-queries`` queries with >= 10 judgments. ESCI is English-only, so this ranks the candidates
on English; the Arabic comparison comes from the eval harness slices.

Writes models/reports/reranker_bench.json and prints a markdown table for models/README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any

import numpy as np

from souq_common.embeddings import repo_root
from souq_models.esci import load_judged_queries, load_products
from souq_models.metrics import ndcg_at_k
from souq_models.registry import ARTIFACTS, MODELS
from souq_search.pipeline import rerank_text
from souq_search.rerank import OnnxCrossEncoder

BATCHES = [1, 8, 16, 30]
REPORT = repo_root() / "models" / "reports" / "reranker_bench.json"


def fp32_view(key: str) -> Path:
    """A model dir pointing at the downloaded fp32 graph (symlinks), loadable by OnnxCrossEncoder."""
    spec = MODELS[key]
    view = ARTIFACTS / f"{key}-fp32"
    view.mkdir(parents=True, exist_ok=True)
    onnx_dir = (spec.src_dir / spec.onnx).parent
    for f in onnx_dir.iterdir():
        if f.name.startswith("model.onnx"):
            link = view / f.name
            if not link.exists():
                link.symlink_to(f)
    tok = view / "tokenizer.json"
    if not tok.exists():
        tok.symlink_to(spec.src_dir / spec.tokenizer)
    int8_meta = ARTIFACTS / f"{key}-int8" / "souq_model.json"
    meta = json.loads(int8_meta.read_text()) if int8_meta.exists() else {"max_length": spec.max_length}
    meta.update(name=key, quantization="none (fp32)", transformer_fusion=False)
    (view / "souq_model.json").write_text(json.dumps(meta, indent=2) + "\n")
    return view


def cpu_name() -> str:
    try:
        return subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return platform.processor() or platform.machine()


def build_pairs(
    n_queries: int, seed: int
) -> tuple[list[tuple[str, dict[str, int]]], dict[str, dict[str, Any]]]:
    queries = [q for q in load_judged_queries(min_judged=10)]
    rng = random.Random(seed)
    rng.shuffle(queries)
    products = load_products(columns=["sku", "title_en", "title_ar", "brand"])
    picked = [(q.query, q.judgments) for q in queries[:n_queries]]
    return picked, products


def bench_latency(
    model: OnnxCrossEncoder, samples: list[tuple[str, list[str]]], runs: int
) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for b in BATCHES:
        times, lengths = [], []
        for i in range(runs + 3):
            query, docs = samples[i % len(samples)]
            batch = (docs * ((b // max(len(docs), 1)) + 1))[:b]
            enc = model._tokenizer.encode_batch([(query, d) for d in batch])
            lengths.append(float(np.mean([len(e.ids) for e in enc])))
            t = time.perf_counter()
            model.score(query, batch)
            if i >= 3:
                times.append((time.perf_counter() - t) * 1000)
        times.sort()
        out[str(b)] = {
            "p50_ms": round(statistics.median(times), 1),
            "p95_ms": round(times[min(len(times) - 1, int(0.95 * len(times)))], 1),
            "mean_pair_tokens": round(float(np.mean(lengths)), 1),
        }
    return out


def bench_quality(
    model: OnnxCrossEncoder,
    picked: list[tuple[str, dict[str, int]]],
    products: dict[str, dict[str, Any]],
    seed: int,
) -> dict[str, float]:
    rng = random.Random(seed)
    model_scores, base_scores = [], []
    for query, judgments in picked:
        skus = [s for s in judgments if s in products]
        rng.shuffle(skus)
        skus = skus[:30]
        if not skus:
            continue
        scores = model.score(query, [rerank_text(products[s], "en") for s in skus])
        ranked = [skus[i] for i in np.argsort(-scores, kind="stable")]
        model_scores.append(ndcg_at_k(ranked, {s: judgments[s] for s in skus}))
        base_scores.append(ndcg_at_k(skus, {s: judgments[s] for s in skus}))
    return {
        "ndcg@10": round(float(np.mean(model_scores)), 4),
        "shuffled_baseline_ndcg@10": round(float(np.mean(base_scores)), 4),
        "n_queries": len(model_scores),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--variants",
        nargs="*",
        default=[
            "mmarco-mMiniLMv2-L12-H384-v1-int8",
            "mmarco-mMiniLMv2-L12-H384-v1-fp32",
            "bge-reranker-v2-m3-int8",
            "bge-reranker-v2-m3-fp32",
        ],
    )
    parser.add_argument("--threads", type=int, default=int(os.environ.get("SOUQ_ORT_THREADS", "4")))
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--quality-queries", type=int, default=100)
    parser.add_argument("--max-length", type=int, default=None, help="override the model's max_length")
    parser.add_argument("--chunk", type=int, default=None, help="length-sorted sub-batch size")
    parser.add_argument("--tag", default="", help="suffix for the report key (e.g. 'loaded')")
    parser.add_argument("--seed", type=int, default=13)
    parser.add_argument("--max-load", type=float, default=None, help="wait for 1-min load avg below this")
    parser.add_argument("--max-wait", type=float, default=600, help="seconds to wait for --max-load")
    parser.add_argument("--out", type=Path, default=REPORT)
    args = parser.parse_args()

    picked, products = build_pairs(args.quality_queries, args.seed)
    samples = [
        (q, [rerank_text(products[s], "en") for s in list(j)[:30] if s in products]) for q, j in picked[:20]
    ]
    report: dict[str, Any] = json.loads(args.out.read_text()) if args.out.exists() else {"variants": {}}
    report["machine"] = {"cpu": cpu_name(), "cores": os.cpu_count(), "ort_threads": args.threads}
    for variant in args.variants:
        if args.max_load is not None:
            # Other processes on the box distort CPU latency; wait (bounded) for a quieter moment.
            waited = 0.0
            while os.getloadavg()[0] > args.max_load and waited < args.max_wait:
                time.sleep(10)
                waited += 10
        key, _, kind = variant.rpartition("-")
        model_dir = fp32_view(key) if kind == "fp32" else ARTIFACTS / variant
        if not (model_dir / "model.onnx").exists():
            print(f"skip {variant}: {model_dir}/model.onnx missing")
            continue
        t = time.perf_counter()
        model = OnnxCrossEncoder(
            model_dir, max_length=args.max_length, threads=args.threads, chunk=args.chunk
        )
        load_s = time.perf_counter() - t
        size_mb = sum(
            f.stat().st_size for f in model_dir.resolve().iterdir() if f.name.startswith("model.onnx")
        )
        if kind == "fp32":
            onnx_dir = (MODELS[key].src_dir / MODELS[key].onnx).parent
            size_mb = sum(f.stat().st_size for f in onnx_dir.iterdir() if f.name.startswith("model.onnx"))
        entry = {
            "max_length": model.max_length,
            "chunk": model.chunk,
            "threads": args.threads,
            "load_avg_1m": round(os.getloadavg()[0], 1),
            "size_mb": round(size_mb / 1e6),
            "load_s": round(load_s, 1),
            "latency": bench_latency(model, samples, args.runs),
            "quality": bench_quality(model, picked, products, args.seed),
        }
        report["variants"][f"{variant}@len{model.max_length}/t{args.threads}/c{model.chunk}{args.tag}"] = (
            entry
        )
        print(variant, json.dumps(entry))
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n")

    head = " | ".join(f"b={b} p50/p95 ms" for b in BATCHES)
    print(f"\n| variant | size MB | {head} | nDCG@10 | load avg |")
    print("|---|---|" + "---|" * len(BATCHES) + "---|---|")
    for name, e in report["variants"].items():
        lat = " | ".join(
            f"{e['latency'][str(b)]['p50_ms']} / {e['latency'][str(b)]['p95_ms']}" for b in BATCHES
        )
        print(f"| {name} | {e['size_mb']} | {lat} | {e['quality']['ndcg@10']} | {e.get('load_avg_1m', '')} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

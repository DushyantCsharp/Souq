"""Does dynamic int8 bge-m3 hold quality against the fp32 graph?

    uv run --package souq-models python models/scripts/check_embedder_quality.py [--queries 100]

1. Agreement: cosine(fp32, int8) for the same text, over ESCI queries, product documents
   (``souq_common.embeddings.document_text``) and a fixed set of Arabic / Arabizi / mixed queries.
2. Retrieval: for each ESCI query, rank its judged products by cosine with fp32 and with int8 vectors;
   nDCG@10 for both and the top-10 overlap.
3. Latency: single-query encode p50/p95 (the search path's ``embed`` stage).

Writes models/reports/embedder_quality.json. The service uses int8 when the nDCG@10 drop is < 0.01 and the
mean agreement cosine is > 0.98 (see models/README.md).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import time
from pathlib import Path
from typing import Any

import numpy as np

from souq_common.embeddings import OnnxBgeM3Embedder, document_text, repo_root
from souq_models.esci import load_judged_queries, load_products
from souq_models.metrics import ndcg_at_k
from souq_models.registry import ARTIFACTS, MODELS

REPORT = repo_root() / "models" / "reports" / "embedder_quality.json"
EXTRA_QUERIES = [
    "سماعات بلوتوث لاسلكية",
    "حقيبة ظهر للابتوب",
    "شاحن ايفون سريع",
    "ساعة ذكية رياضية",
    "كفر جوال سامسونج",
    "حذاء رياضي رجالي",
    "مكنسة كهربائية",
    "طاولة قهوة خشب",
    "sama3at bluetooth",
    "shanta laptop",
    "jazma riyadiya",
    "جراب iphone 13",
    "شاحن type c سريع",
    "wireles headphnes",
    "labtop bag 15 inch",
]


def fp32_dir() -> Path:
    spec = MODELS["bge-m3"]
    view = ARTIFACTS / "bge-m3-fp32"
    view.mkdir(parents=True, exist_ok=True)
    onnx_dir = (spec.src_dir / spec.onnx).parent
    for f in onnx_dir.iterdir():
        if f.name.startswith("model.onnx") or f.name == "Constant_7_attr__value":
            link = view / f.name
            if not link.exists():
                link.symlink_to(f)
    tok = view / "tokenizer.json"
    if not tok.exists():
        tok.symlink_to(spec.src_dir / spec.tokenizer)
    return view


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--queries", type=int, default=100)
    parser.add_argument("--threads", type=int, default=int(os.environ.get("SOUQ_ORT_THREADS", "4")))
    parser.add_argument("--seed", type=int, default=13)
    parser.add_argument("--out", type=Path, default=REPORT)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    judged = load_judged_queries(min_judged=10)
    rng.shuffle(judged)
    judged = judged[: args.queries]
    products = load_products(columns=["sku", "title_en", "title_ar", "brand", "category_path", "colour"])
    for p in products.values():
        p["category_path"] = p.get("category_path")

    fp32 = OnnxBgeM3Embedder(fp32_dir(), threads=args.threads)
    int8 = OnnxBgeM3Embedder(ARTIFACTS / "bge-m3-int8", threads=args.threads)

    query_texts = [q.query for q in judged] + EXTRA_QUERIES
    skus = sorted({s for q in judged for s in q.judgments if s in products})
    doc_texts = [document_text(products[s]) for s in skus]

    t = time.perf_counter()
    q32, q8 = fp32.encode_queries(query_texts), int8.encode_queries(query_texts)
    d32, d8 = fp32.encode_documents(doc_texts), int8.encode_documents(doc_texts)
    encode_s = time.perf_counter() - t
    agree_q = np.sum(q32 * q8, axis=1)
    agree_d = np.sum(d32 * d8, axis=1)

    idx = {s: i for i, s in enumerate(skus)}
    nd32, nd8, overlap = [], [], []
    for qi, q in enumerate(judged):
        cand = [s for s in q.judgments if s in idx]
        if not cand:
            continue
        rows = [idx[s] for s in cand]
        r32 = [cand[i] for i in np.argsort(-(d32[rows] @ q32[qi]), kind="stable")]
        r8 = [cand[i] for i in np.argsort(-(d8[rows] @ q8[qi]), kind="stable")]
        nd32.append(ndcg_at_k(r32, q.judgments))
        nd8.append(ndcg_at_k(r8, q.judgments))
        overlap.append(len(set(r32[:10]) & set(r8[:10])) / max(1, min(10, len(cand))))

    lat: dict[str, Any] = {}
    for name, model in (("fp32", fp32), ("int8", int8)):
        times = []
        for i in range(43):
            t = time.perf_counter()
            model.encode_queries([query_texts[i % len(query_texts)]])
            if i >= 3:
                times.append((time.perf_counter() - t) * 1000)
        times.sort()
        lat[name] = {
            "p50_ms": round(statistics.median(times), 1),
            "p95_ms": round(times[int(0.95 * len(times))], 1),
        }

    report = {
        "n_queries": len(query_texts),
        "n_docs": len(doc_texts),
        "agreement_cosine": {
            "queries_mean": round(float(agree_q.mean()), 4),
            "queries_min": round(float(agree_q.min()), 4),
            "docs_mean": round(float(agree_d.mean()), 4),
            "docs_min": round(float(agree_d.min()), 4),
            "extra_queries": {
                q: round(float(c), 4)
                for q, c in zip(EXTRA_QUERIES, agree_q[-len(EXTRA_QUERIES) :], strict=True)
            },
        },
        "retrieval": {
            "fp32_ndcg@10": round(float(np.mean(nd32)), 4),
            "int8_ndcg@10": round(float(np.mean(nd8)), 4),
            "top10_overlap": round(float(np.mean(overlap)), 4),
            "n_queries": len(nd32),
        },
        "query_latency_ms": lat,
        "threads": args.threads,
        "load_avg_1m": round(os.getloadavg()[0], 1),
        "encode_seconds_total": round(encode_s, 1),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

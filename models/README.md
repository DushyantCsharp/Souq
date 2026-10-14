# models

ONNX artefacts served in-process by `services/search` (reranker, query embeddings) and by the indexer /
`scripts/reindex.py` (document embeddings). Everything under `models/artifacts/` is gitignored; what is
committed is the export code, the measured reports in `models/reports/`, and `models/checksums.txt`.

```
uv run --package souq-models python models/scripts/export.py --all --install     # download + int8 export
uv run --package souq-models python models/scripts/bench_rerankers.py --max-load 6
uv run --package souq-models python models/scripts/check_embedder_quality.py
uv run --package souq-models python models/scripts/write_checksums.py            # --check to verify
uv run --package souq-models python models/scripts/package_release.py            # release tarballs
```

## Pipeline

1. **Download** (`souq_models.hub.fetch`): every file pinned to a hub commit SHA (`souq_models.registry`),
   resumable, and checked against the hub's LFS sha256 and size.
2. **Fuse** (`onnx_tools.optimize_fp32`): onnxruntime's own graph optimizer at the *extended* level, saved
   offline (LayerNorm/GELU/attention fusions where the exporter's pattern matches). No torch needed.
3. **Quantise** (`onnx_tools.quantize_int8`): onnxruntime dynamic quantisation, QInt8 weights, per-tensor,
   `MatMulConstBOnly` (activation × activation matmuls in attention stay fp32).
4. **Install**: the chosen models are copied to the runtime dirs the services load —
   `artifacts/reranker-int8/` (`SOUQ_RERANKER_MODEL`) and `artifacts/bge-m3-int8/` (`SOUQ_EMBEDDER_MODEL`),
   each `model.onnx` + `tokenizer.json` + `souq_model.json` (name, pinned revision, max_length, pad id).

| key | source (pinned) | params | fp32 → int8 |
|---|---|---|---|
| `mmarco-mMiniLMv2-L12-H384-v1` | `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1@1427fd65` `onnx/model.onnx` | 118M (96M of it the 250k-token embedding table) | 471 → 119 MB |
| `bge-reranker-v2-m3` | `onnx-community/bge-reranker-v2-m3-ONNX@6f5ff652` (export of `BAAI/bge-reranker-v2-m3`) | 568M | 2271 → 571 MB |
| `bge-m3` | `BAAI/bge-m3@5617a9f6` `onnx/model.onnx` (official, outputs `sentence_embedding`) | 568M | 2267 → 566 MB |

## Reranker choice

Measured with `scripts/bench_rerankers.py --max-load 6 --tag quiet` on an Apple M-series laptop
(10 cores, 4 ORT threads, load average 1.9–3.7, max_length 128). Latency is one `score()` call including
tokenisation; quality is nDCG@10 reranking each ESCI query's judged products (100 queries, English only).

| variant | size MB | b=1 p50/p95 ms | b=8 | b=16 | **b=30 (top-30)** | nDCG@10 |
|---|---|---|---|---|---|---|
| **mMiniLM-L12 int8** (default) | 119 | 2.5 / 4.4 | 23.8 / 39.0 | 44.9 / 93.5 | **72.2 / 105.1** | 0.798 |
| mMiniLM-L12 fp32 | 471 | 3.3 / 4.6 | 25.0 / 40.0 | 52.8 / 68.1 | 88.3 / 125.3 | 0.799 |
| bge-reranker-v2-m3 int8 | 572 | 16.2 / 25.2 | 158.0 / 250.3 | 368.9 / 533.9 | 537.1 / 766.3 | 0.817 |
| bge-reranker-v2-m3 fp32 | 2272 | 32.3 / 44.6 | 267.3 / 428.9 | 563.4 / 756.3 | 924.5 / 1287.4 | 0.820 |
| (unreranked, shuffled) | | | | | | 0.703 |

**Default: mMiniLM-L12 int8.** It is the only candidate whose top-30 p95 (105 ms) fits the 120 ms
`search.rerank_ms` budget. bge-reranker-v2-m3 int8 is ~2 nDCG points better here and in the eval harness
(RRF 0.466 → 0.487 overall), but at 537 ms p50 it is ~4.5× over budget on CPU; it becomes viable only with
a GPU or a smaller window (b=8 p95 250 ms is still over). int8 costs nothing measurable on quality.

Under contention the budget does not hold: the same mMiniLM int8 top-30 measured 194 / 562 ms (p50/p95) at
load average 28, and 8 threads is worse than 4 when cores are oversubscribed (529 / 1788 ms). The service
therefore degrades to the fused order (`degraded: ["rerank"]`) instead of queueing; see the k6 report
(`docs/perf-report.md`) for the degraded share at 200 RPS.

## Embedder: bge-m3 int8

bge-m3 dense, 1024 dims, CLS pooling + L2 norm (`souq_common.embeddings.OnnxBgeM3Embedder`), int8 dynamic
quantisation. From `scripts/check_embedder_quality.py` (`reports/embedder_quality.json`):

| | fp32 | int8 |
|---|---|---|
| retrieval nDCG@10 (100 ESCI queries, 1,784 docs) | 0.803 | 0.806 |
| top-10 overlap vs fp32 | | 0.954 |
| cosine agreement vs fp32 (queries mean / min) | | 0.988 / 0.982 |
| cosine agreement vs fp32 (docs mean / min) | | 0.985 / 0.976 |
| query latency p50 / p95 (4 threads, load avg 30) | 1046 / 5180 ms | 20 / 59 ms |
| size | 2267 MB | 566 MB |

int8 matches fp32 retrieval quality and is the only variant that fits the 30 ms `search.embed_ms` budget.
Document backfill throughput with int8: ~30–44 docs/s on 4 threads (the 50k-product `products_v2` backfill
took ~17 min). Use `SOUQ_ORT_THREADS=4`; 8 threads stalled to ~2 docs/s when the host was oversubscribed.

## Release artefacts and checksums (CI)

`models/checksums.txt` is `sha256sum` format with paths relative to `models/artifacts/`
(`reranker-int8/{model.onnx,tokenizer.json,souq_model.json}`, `bge-m3-int8/{…}`). `souq_model.json` records
the export time, so re-exporting changes its digest: re-run `write_checksums.py` after every export and
commit the manifest together with the new release.

1. `export.py --all --install`, then `write_checksums.py`.
2. `package_release.py` → `models/artifacts/release/{reranker-int8,bge-m3-int8}.tar.gz` (each tarball holds
   `<dir>/...`).
3. `gh release create models-v1 models/artifacts/release/*.tar.gz --title "Search models v1" --notes "..."`
   (bump the tag, e.g. `models-v2`, whenever the manifest changes; CI reads `MODEL_RELEASE_TAG`).
4. CI downloads the tarballs, extracts them and runs
   `python3 ci/verify_checksums.py --manifest models/checksums.txt --root <dir> --require-all --no-extra`.

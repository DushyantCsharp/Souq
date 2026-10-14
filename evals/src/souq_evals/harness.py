"""Ablation harness: runs the real SearchPipeline in-process against the eval index.

Ablations: bm25 (BM25 only), knn (kNN only), rrf (both, fused), rrf_rerank (+ cross-encoder), and in
``full`` mode rrf_rerank_llm (+ LLM query parsing). Metrics per language slice: nDCG@10, Recall@100, MRR,
zero-result rate; filter-parse exact match; per-stage latency p50/p95; LLM path share, tokens and cost.

Stage budgets are multiplied by ``budget_scale`` (default 50) so quality numbers don't depend on how busy
the machine is; timings are still measured and reported, and every degraded response is counted.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from souq_common.budgets import Budgets, load_budgets
from souq_common.embeddings import get_embedder, resolve_model_dir
from souq_common.settings import get_settings
from souq_evals import metrics as m
from souq_evals.corpus import build_corpus, embed_corpus, ensure_index
from souq_evals.golden import SLICE_LANG, GoldenQuery, load_golden
from souq_evals.paths import FILTER_CASES
from souq_search.models import Filters, SearchOptions, SearchRequest
from souq_search.opensearch import OpenSearch
from souq_search.pagination import SessionStore
from souq_search.parsing.lexicon import install_catalog_values
from souq_search.parsing.llm import LlmQueryParser
from souq_search.parsing.llm_fixtures import AnthropicCaller, LlmTransport
from souq_search.parsing.rules import parse_rules
from souq_search.pipeline import (
    ALL_RETRIEVERS,
    BackendUnavailable,
    EmbedService,
    PipelineOptions,
    SearchPipeline,
)
from souq_search.rerank import OnnxCrossEncoder, Reranker
from souq_search.retrieval.backend import OpenSearchBackend

FILTER_FIELDS = ("price_min", "price_max", "currency", "brands", "colours", "sizes", "category")


def _opts(retrievers: frozenset[str], rerank: bool, llm: bool) -> PipelineOptions:
    return PipelineOptions(
        retrievers=retrievers,
        rerank=rerank,
        llm=llm,
        semantic_cache=False,
        debug=True,
        store_session=False,
        log_query=False,
    )


ABLATIONS: dict[str, PipelineOptions] = {
    "bm25": _opts(frozenset({"bm25"}), rerank=False, llm=False),
    "knn": _opts(frozenset({"knn"}), rerank=False, llm=False),
    "rrf": _opts(ALL_RETRIEVERS, rerank=False, llm=False),
    "rrf_rerank": _opts(ALL_RETRIEVERS, rerank=True, llm=False),
    "rrf_rerank_llm": _opts(ALL_RETRIEVERS, rerank=True, llm=True),
}
OFFLINE_ABLATIONS = ["bm25", "knn", "rrf", "rrf_rerank"]
FULL_ABLATIONS = [*OFFLINE_ABLATIONS, "rrf_rerank_llm"]


@dataclass
class EvalConfig:
    mode: str = "offline"
    ablations: list[str] = field(default_factory=lambda: list(OFFLINE_ABLATIONS))
    index: str = "souq_eval"
    distractors: int = 5000
    rebuild_index: bool = False
    budget_scale: float = 50.0
    limit: int = 50
    overrides: dict[str, Any] = field(default_factory=dict)  # search.* config overrides (experiments)


def scaled_budgets(scale: float, overrides: dict[str, Any] | None = None) -> Budgets:
    data = dict(load_budgets().search.as_dict())
    for key, value in list(data.items()):
        if key.endswith("_ms") and isinstance(value, int | float):
            data[key] = value * scale
    data.update(overrides or {})
    return Budgets(data)


def git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return os.environ.get("GITHUB_SHA", "unknown")


def ranking(debug: dict[str, Any], ablation: str) -> list[str]:
    if ablation == "bm25":
        return [s for s, _ in debug.get("bm25", [])]
    if ablation == "knn":
        return [s for s, _ in debug.get("knn", [])]
    window = [s for s, _ in debug.get("window", [])]
    fused = [s for s, _ in debug.get("fused", [])]
    return window + fused[len(window) :]


def _norm_filters(f: Filters | dict[str, Any]) -> dict[str, Any]:
    d = f if isinstance(f, dict) else f.model_dump()
    out: dict[str, Any] = {}
    for k in FILTER_FIELDS:
        v = d.get(k)
        if k in ("brands", "colours", "sizes"):
            out[k] = sorted({str(x).lower() for x in (v or [])})
        elif k in ("price_min", "price_max"):
            out[k] = None if v is None else round(float(v), 2)
        else:
            out[k] = v
    return out


def score_filters(cases: list[dict[str, Any]], predicted: list[Filters | dict[str, Any]]) -> dict[str, Any]:
    exact = 0
    per_field: Counter[str] = Counter()
    misses = []
    for case, pred in zip(cases, predicted, strict=True):
        p, e = _norm_filters(pred), _norm_filters(case["expected"])
        same = [k for k in FILTER_FIELDS if p[k] == e[k]]
        per_field.update(same)
        if len(same) == len(FILTER_FIELDS):
            exact += 1
        else:
            misses.append(case["id"])
    n = len(cases)
    fields = {"price": (per_field["price_min"] + per_field["price_max"]) / (2 * n) if n else 0.0}
    fields.update(
        {k: per_field[k] / n for k in ("currency", "brands", "colours", "sizes", "category")} if n else {}
    )
    return {
        "exact_match": round(exact / n, 4) if n else 0.0,
        "n": n,
        "per_field": {
            (
                "brand" if k == "brands" else "colour" if k == "colours" else "size" if k == "sizes" else k
            ): round(v, 4)
            for k, v in fields.items()
        },
        "misses": misses,
    }


def load_filter_cases(path: Path = FILTER_CASES) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class Harness:
    def __init__(self, cfg: EvalConfig) -> None:
        self.cfg = cfg
        self.settings = get_settings()
        self.notes: list[str] = []

    def _llm(self) -> LlmQueryParser | None:
        if self.cfg.mode != "full":
            return None
        s = self.settings
        caller = (
            AnthropicCaller(s.anthropic_api_key)
            if s.souq_llm_mode != "replay" and s.anthropic_api_key
            else None
        )
        b = load_budgets().search
        return LlmQueryParser(
            LlmTransport(s.souq_llm_mode, caller=caller),
            s.souq_llm_model,
            max_output_tokens=int(b.llm_max_output_tokens),
            token_budget=int(b.llm_tokens_per_query),
        )

    def _reranker(self) -> tuple[Reranker, str]:
        path = resolve_model_dir(self.settings.souq_reranker_model)
        if not (path / "model.onnx").exists():
            self.notes.append(f"no reranker at {path}: rerank ablations degrade to fused order")
            return Reranker(None), "missing"
        model = OnnxCrossEncoder(path)
        return Reranker(model, slots=1), model.name

    async def build_pipeline(self, golden: list[GoldenQuery]) -> tuple[SearchPipeline, dict[str, Any]]:
        embedder = get_embedder()
        embedder_name = self.settings.souq_embedder
        if embedder_name == "fake":
            self.notes.append("SOUQ_EMBEDDER=fake: kNN numbers are a hash-vector baseline, not bge-m3")
        corpus = build_corpus(golden, self.cfg.distractors)
        t = time.perf_counter()
        vectors = embed_corpus(corpus.docs, embedder, embedder_name)
        embed_s = time.perf_counter() - t
        rebuilt = ensure_index(
            self.settings.opensearch_url,
            self.cfg.index,
            corpus,
            vectors,
            embedder_name,
            rebuild=self.cfg.rebuild_index,
        )
        client = OpenSearch(self.settings.opensearch_url)
        brands = await client.terms(self.cfg.index, "brand.kw", 5000)
        colours = await client.terms(self.cfg.index, "colour", 200)
        sizes = await client.terms(self.cfg.index, "size", 500)
        install_catalog_values(brands=brands, colours=set(colours), sizes=set(sizes))
        coverage = await client.coverage(self.cfg.index, ["brand.kw", "colour", "size"])
        coverage["brand"] = coverage.pop("brand.kw", 1.0)
        reranker, reranker_name = self._reranker()
        budgets = scaled_budgets(self.cfg.budget_scale, self.cfg.overrides)
        pipeline = SearchPipeline(
            backend=OpenSearchBackend(client, self.cfg.index, str(budgets.pit_keep_alive)),
            budgets=budgets,
            embed=EmbedService(embedder),
            reranker=reranker,
            llm=self._llm(),
            cache=None,
            sessions=SessionStore(None, 60),
        )
        pipeline.coverage = coverage
        # First ONNX runs allocate and JIT kernels; keep that out of the measured stages.
        for text in ("warm up", "تسخين", "sama3at"):
            await pipeline.embed.embed(text, 30.0)
        if reranker.available:
            await reranker.score("warm up", ["a product title", "another product"] * 15, 30.0)
        info = {
            "index": self.cfg.index,
            "corpus_docs": len(corpus.docs),
            "corpus_fingerprint": corpus.fingerprint,
            "distractors": self.cfg.distractors,
            "index_rebuilt": rebuilt,
            "embedder": embedder_name,
            "reranker": reranker_name,
            "doc_embedding_s": round(embed_s, 1),
            "field_coverage": coverage,
            "budget_scale": self.cfg.budget_scale,
            "overrides": self.cfg.overrides,
            "title_ar_coverage": round(
                sum(1 for d in corpus.docs if d.get("title_ar")) / len(corpus.docs), 4
            ),
        }
        return pipeline, info

    async def run(self) -> dict[str, Any]:
        golden = load_golden()
        pipeline, info = await self.build_pipeline(golden)
        per_query: dict[str, list[dict[str, Any]]] = defaultdict(list)
        stage_times: dict[str, list[float]] = defaultdict(list)
        degraded: dict[str, Counter[str]] = defaultdict(Counter)
        llm_rows: list[dict[str, Any]] = []
        prod = "rrf_rerank_llm" if "rrf_rerank_llm" in self.cfg.ablations else "rrf_rerank"
        for ablation in self.cfg.ablations:
            opts = ABLATIONS[ablation]
            if ablation == prod and pipeline.embed is not None:
                pipeline.embed.clear()  # report real embed latency, not LRU hits from earlier ablations
            for gq in golden:
                req = SearchRequest(
                    q=gq.q,
                    lang=SLICE_LANG.get(gq.slice, "auto"),
                    limit=self.cfg.limit,
                    options=SearchOptions(llm=opts.llm, rerank=opts.rerank, debug=True),
                )
                try:
                    resp = await pipeline.search(req, opts)
                except BackendUnavailable:
                    # every retriever of this ablation failed for this query: score it as an empty result
                    degraded[ablation]["unavailable"] += 1
                    per_query[ablation].append(
                        {
                            "slice": gq.slice,
                            "ndcg@10": 0.0,
                            "mrr": 0.0,
                            "zero": True,
                            "recall@100": m.recall_at_k([], gq.relevant(), 100),
                        }
                    )
                    continue
                ranked = ranking(resp.debug or {}, ablation)
                per_query[ablation].append(
                    {
                        "slice": gq.slice,
                        "ndcg@10": m.ndcg_at_k(ranked, gq.grades(), 10),
                        "recall@100": m.recall_at_k(ranked, gq.relevant(), 100),
                        "mrr": m.reciprocal_rank(ranked, gq.relevant()),
                        "zero": not resp.items,
                    }
                )
                degraded[ablation].update(resp.degraded)
                if ablation == prod:
                    for stage_name, ms in resp.timings_ms.items():
                        stage_times[stage_name].append(ms)
                if ablation == "rrf_rerank_llm":
                    llm = (resp.debug or {}).get("llm") or {}
                    llm_rows.append({"parser": resp.parsed_query.parser, **llm})
        report_slices: dict[str, dict[str, Any]] = {}
        for slice_name in [*sorted({q.slice for q in golden}), "all"]:
            report_slices[slice_name] = {}
            for ablation, rows in per_query.items():
                sel = [r for r in rows if slice_name == "all" or r["slice"] == slice_name]
                report_slices[slice_name][ablation] = {
                    "ndcg@10": m.mean([r["ndcg@10"] for r in sel]),
                    "recall@100": m.mean([r["recall@100"] for r in sel]),
                    "mrr": m.mean([r["mrr"] for r in sel]),
                    "zero_result_rate": m.mean([1.0 if r["zero"] else 0.0 for r in sel]),
                    "n": len(sel),
                }
        filter_parse = await self._filter_parse(pipeline)
        await pipeline.backend.client.aclose()
        return {
            "slices": report_slices,
            "filter_parse": filter_parse,
            "latency_ms": {
                s: {"p50": m.percentile(v, 50), "p95": m.percentile(v, 95)} for s, v in stage_times.items()
            },
            "llm": self._llm_summary(llm_rows, len(golden)),
            "degraded": {a: dict(c) for a, c in degraded.items()},
            "labels": label_summary(golden),
            "setup": info,
            "notes": self.notes,
        }

    async def _filter_parse(self, pipeline: SearchPipeline) -> dict[str, Any]:
        cases = load_filter_cases()
        rule_preds = [parse_rules(c["q"], c["lang"]).filters for c in cases]
        rule = score_filters(cases, rule_preds)
        out: dict[str, Any] = {**rule, "mode": "rule", "rule_only": rule}
        if self.cfg.mode == "full" and pipeline.llm is not None:
            preds: list[Filters] = []
            degraded = 0
            for c in cases:
                parsed, deg = await pipeline.parse(
                    SearchRequest(q=c["q"], lang=c["lang"]), ABLATIONS["rrf_rerank_llm"]
                )
                preds.append(parsed.filters)
                degraded += "llm_parse" in deg
            with_llm = score_filters(cases, preds)
            out = {**with_llm, "mode": "rule+llm", "rule_only": rule, "llm_degraded": degraded}
        out["needs_llm_cases"] = sum(1 for c in cases if c.get("needs_llm"))
        return out

    def _llm_summary(self, rows: list[dict[str, Any]], n_queries: int) -> dict[str, Any]:
        if not rows:
            return {"path_share": 0.0, "avg_tokens": 0.0, "usd_per_1k_queries": 0.0, "enabled": False}
        llm = [r for r in rows if r["parser"] in ("llm", "cache")]
        tokens = [r.get("input_tokens", 0) + r.get("output_tokens", 0) for r in llm if r["parser"] == "llm"]
        cost = sum(r.get("cost_usd") or 0.0 for r in rows)
        sources = Counter(
            str((r.get("recorded_at") == "synthetic" and "synthetic") or r.get("source")) for r in llm
        )
        return {
            "path_share": round(len(llm) / len(rows), 4),
            "avg_tokens": round(sum(tokens) / len(tokens), 1) if tokens else 0.0,
            "usd_per_1k_queries": round(cost / max(1, n_queries) * 1000, 6),
            "enabled": True,
            "model": self.settings.souq_llm_model,
            "llm_mode": self.settings.souq_llm_mode,
            "fixture_provenance": dict(sources),
        }


def label_summary(golden: list[GoldenQuery]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for slice_name in sorted({q.slice for q in golden}):
        counts: Counter[str] = Counter()
        for q in golden:
            if q.slice == slice_name:
                counts.update(q.label_sources())
        total = sum(counts.values())
        out[slice_name] = {
            **dict(counts),
            "provisional_share": round(counts["provisional"] / total, 4) if total else 0.0,
            "claude_share": round(counts["claude"] / total, 4) if total else 0.0,
        }
    return out

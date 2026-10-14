from __future__ import annotations

from pathlib import Path
from typing import Any

from souq_evals import report as rep
from souq_evals.harness import score_filters


def sample(ndcg: float = 0.5) -> dict[str, Any]:
    abl = {"ndcg@10": ndcg, "recall@100": 0.8, "mrr": 0.6, "zero_result_rate": 0.0, "n": 10}
    results = {
        "slices": {"en": {"rrf": abl, "rrf_rerank": abl}, "all": {"rrf_rerank": abl}},
        "filter_parse": {"exact_match": 0.9, "n": 80, "per_field": {"price": 1.0}, "mode": "rule"},
        "latency_ms": {"bm25": {"p50": 1.0, "p95": 2.0}},
        "llm": {"path_share": 0.0, "avg_tokens": 0.0, "usd_per_1k_queries": 0.0, "enabled": False},
        "labels": {"ar": {"provisional": 10, "provisional_share": 1.0}},
    }
    return rep.finalise("r1", "abc", "offline", results)


def test_report_has_contract_keys_and_flags_provisional() -> None:
    r = sample()
    assert {"run_id", "git_sha", "mode", "slices", "filter_parse", "latency_ms", "llm"} <= set(r)
    assert any("provisional" in w for w in r["warnings"])
    assert "rrf_rerank_llm" not in r["slices"]["en"]  # offline reports omit the LLM ablation


def test_thresholds_gate_best_ablation(tmp_path: Path) -> None:
    spec = tmp_path / "t.yaml"
    spec.write_text(
        "slices:\n  en: {ndcg@10: 0.6, zero_result_rate_max: 0.1}\nfilter_parse: {exact_match: 0.95}\n"
    )
    failures = rep.check_thresholds(sample(0.5), spec)
    assert any("en/rrf_rerank ndcg@10" in f for f in failures)
    assert any("filter_parse" in f for f in failures)
    spec.write_text("slices:\n  en: {ndcg@10: 0.4}\n")
    assert rep.check_thresholds(sample(0.5), spec) == []


def test_markdown_renders() -> None:
    assert "| en | rrf_rerank |" in rep.markdown(sample())


def test_score_filters_exact_and_per_field() -> None:
    cases = [
        {"id": "a", "expected": {"price_max": 300, "colours": ["black"]}},
        {"id": "b", "expected": {"brands": ["Nike"]}},
    ]
    out = score_filters(cases, [{"price_max": 300.0, "colours": ["Black"]}, {"brands": ["adidas"]}])
    assert out["exact_match"] == 0.5
    assert out["misses"] == ["b"]
    assert out["per_field"]["brand"] == 0.5
    assert out["per_field"]["price"] == 1.0

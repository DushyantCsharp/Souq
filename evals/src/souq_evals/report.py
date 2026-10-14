"""Report JSON (schema in CLAUDE.md, consumed by CI), markdown summary, and the thresholds gate."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from souq_evals.paths import REPORTS, THRESHOLDS

GATE_ORDER = ["rrf_rerank_llm", "rrf_rerank", "rrf"]


def finalise(run_id: str, git_sha: str, mode: str, results: dict[str, Any]) -> dict[str, Any]:
    warnings = list(results.get("notes", []))
    for slice_name, labels in results.get("labels", {}).items():
        share = labels.get("provisional_share", 0.0)
        if share > 0:
            warnings.append(
                f"{slice_name}: {share:.0%} of judgments are provisional (translated from ESCI, not yet "
                "hand-labelled by the maintainer) — treat this slice's numbers as indicative"
            )
        claude = labels.get("claude_share", 0.0)
        if claude > 0:
            warnings.append(
                f"{slice_name}: {claude:.0%} of judgments were graded by Claude (a model) at the "
                "maintainer's request, not by a human — they measure agreement with a model's judgement"
            )
    llm = results.get("llm", {})
    if llm.get("fixture_provenance", {}).get("synthetic"):
        warnings.append(
            "LLM path replayed hand-authored synthetic fixtures: llm and rule+llm numbers are not a "
            "measurement of the live model"
        )
    return {
        "run_id": run_id,
        "git_sha": git_sha,
        "mode": mode,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        **{k: results[k] for k in ("slices", "filter_parse", "latency_ms", "llm")},
        "degraded": results.get("degraded", {}),
        "labels": results.get("labels", {}),
        "setup": results.get("setup", {}),
        "warnings": warnings,
    }


def gated_ablation(slice_metrics: dict[str, Any]) -> str | None:
    return next((a for a in GATE_ORDER if a in slice_metrics), None)


def check_thresholds(report: dict[str, Any], path: Path = THRESHOLDS) -> list[str]:
    """Failures as human-readable strings (empty list = pass)."""
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    failures: list[str] = []
    for slice_name, targets in (spec.get("slices") or {}).items():
        metrics = report["slices"].get(slice_name)
        if metrics is None:
            failures.append(f"{slice_name}: slice missing from report")
            continue
        ablation = gated_ablation(metrics)
        if ablation is None:
            failures.append(f"{slice_name}: no gated ablation in report")
            continue
        got = metrics[ablation]
        for key, floor in targets.items():
            if key.endswith("_max"):
                metric = key.removesuffix("_max")
                if got[metric] > floor:
                    failures.append(f"{slice_name}/{ablation} {metric} {got[metric]} > max {floor}")
            elif got[key] < floor:
                failures.append(f"{slice_name}/{ablation} {key} {got[key]} < {floor}")
    fp = spec.get("filter_parse") or {}
    if "exact_match" in fp and report["filter_parse"]["exact_match"] < fp["exact_match"]:
        failures.append(
            f"filter_parse exact_match {report['filter_parse']['exact_match']} < {fp['exact_match']}"
        )
    return failures


def markdown(report: dict[str, Any]) -> str:
    lines = [f"# Eval report `{report['run_id']}` ({report['mode']}, {report['git_sha'][:10]})", ""]
    setup = report.get("setup", {})
    lines.append(
        f"Corpus {setup.get('corpus_docs')} docs (index `{setup.get('index')}`),"
        f" embedder `{setup.get('embedder')}`, reranker `{setup.get('reranker')}`,"
        f" budget scale x{setup.get('budget_scale')}."
    )
    lines += [
        "",
        "| slice | ablation | nDCG@10 | Recall@100 | MRR | zero-result | n |",
        "|---|---|---|---|---|---|---|",
    ]
    for slice_name, ablations in report["slices"].items():
        for ablation, v in ablations.items():
            lines.append(
                f"| {slice_name} | {ablation} | {v['ndcg@10']:.4f} | {v['recall@100']:.4f} "
                f"| {v['mrr']:.4f} | {v['zero_result_rate']:.4f} | {v['n']} |"
            )
    fp = report["filter_parse"]
    lines += [
        "",
        f"Filter parse ({fp.get('mode')}): exact match {fp['exact_match']:.4f} over {fp['n']} cases; "
        f"per field {json.dumps(fp['per_field'])}",
    ]
    if fp.get("mode") == "rule+llm":
        lines.append(f"Rule parser alone: exact match {fp['rule_only']['exact_match']:.4f}.")
    lines += ["", "| stage | p50 ms | p95 ms |", "|---|---|---|"]
    lines += [f"| {s} | {v['p50']} | {v['p95']} |" for s, v in report["latency_ms"].items()]
    llm = report["llm"]
    lines += [
        "",
        f"LLM: path share {llm['path_share']}, avg tokens {llm['avg_tokens']}, "
        f"USD per 1k queries {llm['usd_per_1k_queries']} (enabled: {llm.get('enabled')})",
    ]
    if report.get("degraded"):
        lines += ["", f"Degraded responses per ablation: `{json.dumps(report['degraded'])}`"]
    lines += ["", "Labels: `" + json.dumps(report.get("labels", {})) + "`"]
    if report.get("warnings"):
        lines += ["", "**Warnings**", *[f"- {w}" for w in report["warnings"]]]
    return "\n".join(lines) + "\n"


def write(report: dict[str, Any], out: Path | None = None) -> tuple[Path, Path]:
    out = out or REPORTS / f"{report['run_id']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md = out.with_suffix(".md")
    md.write_text(markdown(report), encoding="utf-8")
    return out, md

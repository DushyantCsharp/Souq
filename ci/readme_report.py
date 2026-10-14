#!/usr/bin/env python3
"""Render the README's "Targets vs measured" and "Cost" tables from the measured reports.

Inputs (paths relative to the repo root; each has a flag to override it):

  evals/reports/*.json              newest ``mode: full`` and newest ``mode: offline`` report, by
                                    ``created_at``
                                    (schema: CLAUDE.md, "Eval report")
  loadtest/results/summary.json     raw k6 ``handleSummary`` data written by loadtest/search.js
  models/reports/reranker_bench.json
  config/pricing.yaml               USD per million tokens, per model
  docs/reports/usage.json           token usage per unit of work (schema below)

``docs/reports/usage.json``::

    {"generated_at": "2026-10-14T09:00:00+00:00",
     "items": [{"unit": "1k searches" | "1k titles translated" | "1k judged pairs",
                "model": "claude-haiku-5-5",      # a key of pricing.yaml `models`
                "input_tokens": 0, "output_tokens": 0,   # totals for ONE unit (e.g. per 1k searches)
                "requests": 0,                    # API requests behind those tokens
                "batch": false,                   # sent through the Message Batches API
                "note": "how it was measured"}]}

Which number feeds which target:

* relevance (nDCG lift over ``bm25``, Recall@100, zero-result rate), filter-parse exact match, LLM-path
  share: the newest full report, else the newest offline report. The "production ablation" is the first
  present of ``rrf_rerank_llm`` > ``rrf_rerank`` > ``rrf`` (as in ci/eval_compare.py).
* tokens per LLM-path query: the ``1k searches`` usage item (live usage) when present, else ``llm.avg_tokens``
  of the full report.
* standard / LLM-path p95: k6. The k6 summary does not record the offered rate. It is taken from
  ``--k6-rps``, else ``setup_data.rate``, else inferred from the ``dropped_iterations`` threshold, which
  search.js sets to ``rate<max(1, RATE*0.01)`` (exact when RATE > 100).
* reranker top-30 p95: ``--reranker-variant`` in the bench report; its ``…quiet``-less twin (same settings
  measured on a loaded host) is shown next to it.
* index freshness: manual (``--freshness-p95-s``, ``--freshness-source``), since no report file holds it yet.

Status: ✅ met · ❌ missed · ⚠️ met only under a caveat stated in the row (labels not human, replayed LLM
fixtures, lower load than the target, quiet CPU only) or not measured.

Usage::

    uv run python ci/readme_report.py                      # print both blocks to stdout
    uv run python ci/readme_report.py --write README.md \\
        --freshness-p95-s 8.3 --freshness-source docs/handoffs/workers-STATUS.md

``--write`` replaces what is between ``<!-- report:targets:start -->`` / ``<!-- report:targets:end -->`` and
``<!-- report:cost:start -->`` / ``<!-- report:cost:end -->``; without it both blocks, markers included, go to
stdout. Missing input files render as "not measured"; malformed inputs or missing markers exit 2.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORTS_DIR = Path("evals/reports")
DEFAULT_K6_SUMMARY = Path("loadtest/results/summary.json")
DEFAULT_RERANKER_BENCH = Path("models/reports/reranker_bench.json")
DEFAULT_PRICING = Path("config/pricing.yaml")
DEFAULT_USAGE = Path("docs/reports/usage.json")
DEFAULT_RERANKER_VARIANT = "mmarco-mMiniLMv2-L12-H384-v1-int8@len128/t4/c8quiet"

PRODUCTION_PRIORITY: tuple[str, ...] = ("rrf_rerank_llm", "rrf_rerank", "rrf")
LANGUAGE_SLICES: tuple[str, ...] = ("en", "ar", "arabizi_mixed")
HUMAN_LABEL_SOURCES = frozenset({"esci", "maintainer"})
# Fallback when pricing.yaml has no batch_multiplier: the Message Batches API bills at 50 % of list price
# (scripts/seed/translate.py). Prefer adding `batch_multiplier` to pricing.yaml.
DEFAULT_BATCH_MULTIPLIER = 0.5
USAGE_UNITS: tuple[str, ...] = ("1k searches", "1k titles translated", "1k judged pairs")
SEARCH_UNIT = "1k searches"

# Product targets (README "Targets"). These are the plan's targets, not the CI regression floors.
TARGET_NDCG_LIFT_POINTS = {"en": 5.0, "ar": 10.0}
TARGET_RECALL_100 = 0.90
TARGET_ZERO_RESULT_MAX = 0.02
TARGET_FILTER_EXACT = 0.92
TARGET_STANDARD_P95_MS = 250.0
TARGET_LOAD_RPS = 200.0
TARGET_ERROR_RATE_MAX = 0.01  # k6 threshold in loadtest/search.js: a p95 with >= 1 % errors is not a pass
TARGET_LLM_P95_MS = 1200.0
TARGET_LLM_SHARE_MAX = 0.10
TARGET_TOKENS_PER_QUERY = 1500.0
TARGET_RERANK_P95_MS = 120.0
RERANK_WINDOW = "30"
TARGET_FRESHNESS_P95_S = 30.0

MARKERS = ("targets", "cost")
NOT_MEASURED = "not measured"

Status = Literal["met", "missed", "caveat", "unmeasured"]
STATUS_ICON: dict[Status, str] = {"met": "✅", "missed": "❌", "caveat": "⚠️", "unmeasured": "⚠️"}
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class InputError(ValueError):
    """An input exists but cannot be used (malformed JSON/YAML, wrong shape, missing markers)."""


@dataclass(frozen=True)
class Row:
    area: str
    target: str
    value: str
    measured: str
    status: Status
    source: str


@dataclass(frozen=True)
class EvalReport:
    path: Path
    data: dict[str, Any]
    created_at: datetime

    @property
    def mode(self) -> str:
        return str(self.data.get("mode", ""))

    @property
    def run_id(self) -> str:
        return str(self.data.get("run_id", self.path.stem))


@dataclass(frozen=True)
class Inputs:
    root: Path
    full: EvalReport | None
    offline: EvalReport | None
    k6: dict[str, Any] | None
    k6_path: Path
    bench: dict[str, Any] | None
    bench_path: Path
    pricing: dict[str, Any] | None
    pricing_text: str
    pricing_path: Path
    usage: dict[str, Any] | None
    usage_path: Path


# ------------------------------------------------------------------------------------------------ loading


def load_json(path: Path) -> dict[str, Any] | None:
    """The JSON object in ``path``; None when the file does not exist."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputError(f"{path}: not valid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise InputError(f"{path}: expected a JSON object")
    return data


def load_yaml(path: Path) -> tuple[dict[str, Any] | None, str]:
    if not path.is_file():
        return None, ""
    text = path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise InputError(f"{path}: not valid YAML ({exc})") from exc
    if not isinstance(data, dict):
        raise InputError(f"{path}: expected a mapping")
    return data, text


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def latest_eval_reports(reports_dir: Path) -> dict[str, EvalReport]:
    """Newest report per mode (``full``/``offline``) by ``created_at``; other JSON files are skipped."""
    newest: dict[str, EvalReport] = {}
    if not reports_dir.is_dir():
        return newest
    for path in sorted(reports_dir.glob("*.json")):
        data = load_json(path)
        if data is None:
            continue
        created = parse_time(data.get("created_at"))
        if created is None or data.get("mode") not in ("full", "offline") or "slices" not in data:
            print(f"readme_report: skipping {path} (no mode/created_at/slices)", file=sys.stderr)
            continue
        report = EvalReport(path, data, created)
        current = newest.get(report.mode)
        if current is None or (created, path.name) > (current.created_at, current.path.name):
            newest[report.mode] = report
    return newest


def load_inputs(
    root: Path,
    reports_dir: Path,
    k6_path: Path,
    bench_path: Path,
    pricing_path: Path,
    usage_path: Path,
) -> Inputs:
    reports = latest_eval_reports(root / reports_dir)
    pricing, pricing_text = load_yaml(root / pricing_path)
    return Inputs(
        root=root,
        full=reports.get("full"),
        offline=reports.get("offline"),
        k6=load_json(root / k6_path),
        k6_path=k6_path,
        bench=load_json(root / bench_path),
        bench_path=bench_path,
        pricing=pricing,
        pricing_text=pricing_text,
        pricing_path=pricing_path,
        usage=load_json(root / usage_path),
        usage_path=usage_path,
    )


def git_short_sha(cwd: Path, run: Runner = subprocess.run) -> str:
    try:
        out = run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() or "unknown"


# ---------------------------------------------------------------------------------------------- helpers


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _rel(inputs: Inputs, path: Path) -> str:
    try:
        return path.resolve().relative_to(inputs.root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _src(*paths: str) -> str:
    return ", ".join(f"`{p}`" for p in paths)


def _pct(value: float) -> str:
    return f"{value * 100:.1f} %"


def _fmt_time(value: datetime | None) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC") if value else "date not recorded"


def cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def production_ablation(slice_metrics: Mapping[str, Any]) -> str | None:
    return next((a for a in PRODUCTION_PRIORITY if isinstance(slice_metrics.get(a), dict)), None)


def primary_report(inputs: Inputs) -> EvalReport | None:
    return inputs.full or inputs.offline


def non_human_label_share(report: EvalReport, slices: Iterable[str]) -> tuple[float, dict[str, int]]:
    """Share of judgments in ``slices`` not labelled by a human (ESCI or the maintainer), and the counts."""
    counts: dict[str, int] = {}
    labels = report.data.get("labels")
    if not isinstance(labels, dict):
        return 0.0, counts
    for name in slices:
        per_slice = labels.get(name)
        if not isinstance(per_slice, dict):
            continue
        for source, n in per_slice.items():
            if not source.endswith("_share") and isinstance(n, int) and not isinstance(n, bool):
                counts[source] = counts.get(source, 0) + n
    total = sum(counts.values())
    other = sum(n for source, n in counts.items() if source not in HUMAN_LABEL_SOURCES)
    return (other / total if total else 0.0), {
        s: n for s, n in counts.items() if s not in HUMAN_LABEL_SOURCES
    }


def _label_note(share: float, counts: Mapping[str, int]) -> str:
    detail = ", ".join(f"{source} {n:,}" for source, n in sorted(counts.items()))
    return f"{share * 100:.0f} % of judgments not human-labelled ({detail})"


def synthetic_llm(report: EvalReport | None) -> bool:
    llm = report.data.get("llm") if report else None
    provenance = llm.get("fixture_provenance") if isinstance(llm, dict) else None
    return isinstance(provenance, dict) and bool(provenance.get("synthetic"))


def _metric(report: EvalReport, slice_name: str, ablation: str, key: str) -> float | None:
    slices = report.data.get("slices")
    per_slice = slices.get(slice_name) if isinstance(slices, dict) else None
    row = per_slice.get(ablation) if isinstance(per_slice, dict) else None
    return _num(row.get(key)) if isinstance(row, dict) else None


def llm_query_count(report: EvalReport) -> tuple[int, int] | None:
    """(LLM-path queries, queries) behind ``llm.path_share``, from the pooled production row."""
    llm = report.data.get("llm")
    share = _num(llm.get("path_share")) if isinstance(llm, dict) else None
    slices = report.data.get("slices")
    pooled = slices.get("all") if isinstance(slices, dict) else None
    ablation = production_ablation(pooled) if isinstance(pooled, dict) else None
    n = _metric(report, "all", ablation, "n") if ablation else None
    if share is None or not n:
        return None
    return round(share * n), int(n)


def _unmeasured(area: str, target: str, value: str, source: str, why: str = "") -> Row:
    return Row(area, target, value, NOT_MEASURED + (f" ({why})" if why else ""), "unmeasured", source)


# ------------------------------------------------------------------------------------------ target rows


def ndcg_lift_row(inputs: Inputs, slice_name: str) -> Row:
    language = {"en": "English", "ar": "Arabic"}[slice_name]
    target = TARGET_NDCG_LIFT_POINTS[slice_name]
    label = f"nDCG@10 lift over BM25, {language} slice"
    value = f"≥ +{target:g} points"
    report = primary_report(inputs)
    if report is None:
        return _unmeasured("Relevance", label, value, _src("evals/reports/"), "no eval report")
    source = _src(_rel(inputs, report.path))
    slices = report.data.get("slices", {})
    ablation = production_ablation(slices.get(slice_name, {}) if isinstance(slices, dict) else {})
    base = _metric(report, slice_name, "bm25", "ndcg@10")
    current = _metric(report, slice_name, ablation, "ndcg@10") if ablation else None
    if ablation is None or base is None or current is None:
        return _unmeasured("Relevance", label, value, source, f"no bm25/production ablation for {slice_name}")
    lift = (current - base) * 100
    measured = f"{lift:+.1f} points ({ablation} {current:.3f} vs bm25 {base:.3f})"
    status: Status = "met" if lift >= target else "missed"
    share, counts = non_human_label_share(report, [slice_name])
    if share > 0:
        measured += f"; {_label_note(share, counts)}"
        if status == "met":
            status = "caveat"
    return Row("Relevance", label, value, measured, status, source)


def recall_row(inputs: Inputs) -> Row:
    label, value = "Recall@100, all slices", f"≥ {TARGET_RECALL_100:.2f}"
    report = primary_report(inputs)
    if report is None:
        return _unmeasured("Relevance", label, value, _src("evals/reports/"), "no eval report")
    source = _src(_rel(inputs, report.path))
    slices = report.data.get("slices", {})
    ablation = production_ablation(slices.get("all", {}) if isinstance(slices, dict) else {})
    recall = _metric(report, "all", ablation, "recall@100") if ablation else None
    if recall is None or ablation is None:
        return _unmeasured("Relevance", label, value, source, "no production ablation in the all slice")
    measured = f"{recall:.3f} ({ablation})"
    status: Status = "met" if recall >= TARGET_RECALL_100 else "missed"
    share, counts = non_human_label_share(report, LANGUAGE_SLICES)
    if share > 0:
        measured += f"; {_label_note(share, counts)}"
        if status == "met":
            status = "caveat"
    return Row("Relevance", label, value, measured, status, source)


def zero_result_row(inputs: Inputs) -> Row:
    label, value = "Zero-result rate, all slices", f"≤ {TARGET_ZERO_RESULT_MAX * 100:g} %"
    report = primary_report(inputs)
    if report is None:
        return _unmeasured("Relevance", label, value, _src("evals/reports/"), "no eval report")
    source = _src(_rel(inputs, report.path))
    slices = report.data.get("slices", {})
    ablation = production_ablation(slices.get("all", {}) if isinstance(slices, dict) else {})
    rate = _metric(report, "all", ablation, "zero_result_rate") if ablation else None
    if rate is None or ablation is None:
        return _unmeasured("Relevance", label, value, source, "no production ablation in the all slice")
    # Label-independent: no label caveat.
    status: Status = "met" if rate <= TARGET_ZERO_RESULT_MAX else "missed"
    return Row("Relevance", label, value, f"{_pct(rate)} ({ablation})", status, source)


def filter_parse_row(inputs: Inputs) -> Row:
    label, value = "Filter-parse exact match", f"≥ {TARGET_FILTER_EXACT * 100:g} %"
    report = primary_report(inputs)
    fp = report.data.get("filter_parse") if report else None
    if report is None or not isinstance(fp, dict) or _num(fp.get("exact_match")) is None:
        return _unmeasured("Query understanding", label, value, _src("evals/reports/"), "no filter_parse")
    source = _src(_rel(inputs, report.path))
    exact = _num(fp.get("exact_match")) or 0.0
    mode = str(fp.get("mode", "rule"))
    n = fp.get("n")
    measured = f"{_pct(exact)} ({mode}, n={n})"
    rule_only = fp.get("rule_only")
    rule_exact = _num(rule_only.get("exact_match")) if isinstance(rule_only, dict) else None
    if mode != "rule" and rule_exact is not None:
        measured += f"; rule parser alone {_pct(rule_exact)}"
    status: Status = "met" if exact >= TARGET_FILTER_EXACT else "missed"
    if mode != "rule" and synthetic_llm(report):
        measured += "; LLM answers replayed from hand-authored fixtures"
        if status == "met":
            status = "caveat"
    return Row("Query understanding", label, value, measured, status, source)


def k6_metric(summary: Mapping[str, Any], name: str, key: str) -> float | None:
    metrics = summary.get("metrics")
    metric = metrics.get(name) if isinstance(metrics, dict) else None
    values = metric.get("values") if isinstance(metric, dict) else None
    return _num(values.get(key)) if isinstance(values, dict) else None


def k6_offered_rps(summary: Mapping[str, Any], override: float | None = None) -> float | None:
    if override is not None:
        return override
    setup = summary.get("setup_data")
    if isinstance(setup, dict):
        for key in ("rate", "offered_rps"):
            rate = _num(setup.get(key))
            if rate is not None:
                return rate
    metrics = summary.get("metrics")
    dropped = metrics.get("dropped_iterations") if isinstance(metrics, dict) else None
    thresholds = dropped.get("thresholds") if isinstance(dropped, dict) else None
    for expr in thresholds or {}:
        match = re.fullmatch(r"rate<([0-9]+(?:\.[0-9]+)?)", str(expr).replace(" ", ""))
        # search.js: rate<max(1, RATE*0.01). A bound of exactly 1 only says RATE <= 100.
        if match and float(match.group(1)) > 1:
            return float(match.group(1)) * 100
    return None


def k6_started_at(summary: Mapping[str, Any] | None) -> datetime | None:
    setup = summary.get("setup_data") if summary else None
    return parse_time(setup.get("startedAt")) if isinstance(setup, dict) else None


def standard_p95_row(inputs: Inputs, rps_override: float | None) -> Row:
    label = f"Standard path p95 at {TARGET_LOAD_RPS:g} RPS (k6, at the gateway)"
    value = f"≤ {TARGET_STANDARD_P95_MS:g} ms"
    source = _src(inputs.k6_path.as_posix())
    summary = inputs.k6
    p95 = k6_metric(summary, "search_latency_standard", "p(95)") if summary else None
    if summary is None or p95 is None:
        return _unmeasured("Latency", label, value, source, "no k6 summary")
    errors = k6_metric(summary, "http_req_failed", "rate") or 0.0
    achieved = k6_metric(summary, "http_reqs", "rate")
    offered = k6_offered_rps(summary, rps_override)
    load = f"{offered:g} RPS offered" if offered is not None else "offered rate unknown"
    if achieved is not None:
        load += f", {achieved:.1f} achieved"
    measured = f"{p95:,.0f} ms ({load}); errors {_pct(errors)}"
    passing = p95 <= TARGET_STANDARD_P95_MS and errors < TARGET_ERROR_RATE_MAX
    status: Status
    if offered is not None and offered >= TARGET_LOAD_RPS:
        status = "met" if passing else ("missed" if offered <= TARGET_LOAD_RPS else "caveat")
    else:
        status = "missed" if not passing else "caveat"  # failing below the target load is still a miss
    return Row("Latency", label, value, measured, status, source)


def llm_p95_row(inputs: Inputs, llm_mode: str | None) -> Row:
    label, value = "LLM path p95 (k6)", f"≤ {TARGET_LLM_P95_MS / 1000:g} s"
    source = _src(inputs.k6_path.as_posix())
    summary = inputs.k6
    p95 = k6_metric(summary, "search_latency_llm", "p(95)") if summary else None
    if summary is None or p95 is None:
        return _unmeasured("Latency", label, value, source, "no k6 summary")
    share = k6_metric(summary, "llm_path_share", "rate")
    setup = summary.get("setup_data")
    mode = llm_mode or (
        str(setup.get("llm_mode")) if isinstance(setup, dict) and setup.get("llm_mode") else None
    )
    measured = f"{p95:,.0f} ms"
    status: Status = "met" if p95 <= TARGET_LLM_P95_MS else "missed"
    if share is not None and share == 0:
        measured += "; no request took the LLM path (the trend holds long-tail requests that fell back)"
        status = "caveat" if status == "met" else status
    elif mode != "live":
        measured += f"; LLM mode {mode or 'unknown'}, not the live model"
        status = "caveat" if status == "met" else status
    return Row("Latency", label, value, measured, status, source)


def reranker_row(inputs: Inputs, variant: str) -> Row:
    label = f"Reranker top-{RERANK_WINDOW} p95"
    value = f"≤ {TARGET_RERANK_P95_MS:g} ms"
    source = _src(inputs.bench_path.as_posix())
    variants = inputs.bench.get("variants") if inputs.bench else None
    if not isinstance(variants, dict) or not isinstance(variants.get(variant), dict):
        return _unmeasured("Latency", label, value, source, f"variant {variant} not in the bench report")

    def p95_of(entry: Mapping[str, Any]) -> tuple[float | None, float | None]:
        latency = entry.get("latency")
        window = latency.get(RERANK_WINDOW) if isinstance(latency, dict) else None
        p95 = _num(window.get("p95_ms")) if isinstance(window, dict) else None
        return p95, _num(entry.get("load_avg_1m"))

    p95, load = p95_of(variants[variant])
    if p95 is None:
        return _unmeasured("Latency", label, value, source, f"no top-{RERANK_WINDOW} latency for {variant}")
    name = variant.split("@", 1)[0]
    measured = f"{p95:.1f} ms ({name}, load avg {load:g})" if load is not None else f"{p95:.1f} ms ({name})"
    status: Status = "met" if p95 <= TARGET_RERANK_P95_MS else "missed"
    loaded = variants.get(variant.removesuffix("quiet")) if variant.endswith("quiet") else None
    if isinstance(loaded, dict):
        loaded_p95, loaded_load = p95_of(loaded)
        if loaded_p95 is not None:
            measured += f"; {loaded_p95:.1f} ms at load avg {loaded_load:g}"
            if status == "met" and loaded_p95 > TARGET_RERANK_P95_MS:
                status = "caveat"
    return Row("Latency", label, value, measured, status, source)


def llm_share_row(inputs: Inputs) -> Row:
    label, value = "Share of traffic on the LLM path", f"≤ {TARGET_LLM_SHARE_MAX * 100:g} %"
    report = inputs.full
    llm = report.data.get("llm") if report else None
    share = _num(llm.get("path_share")) if isinstance(llm, dict) and llm.get("enabled") else None
    if report is None or share is None:
        return _unmeasured("Cost", label, value, _src("evals/reports/"), "no full eval report with the LLM")
    sources = [_rel(inputs, report.path)]
    counts = llm_query_count(report)
    of = f"{counts[0]} of {counts[1]} golden-set queries" if counts else "golden-set queries"
    measured = f"{_pct(share)} ({of})"
    k6_share = k6_metric(inputs.k6, "llm_path_share", "rate") if inputs.k6 else None
    if k6_share is not None:
        measured += f"; k6 traffic mix {_pct(k6_share)}"
        sources.append(inputs.k6_path.as_posix())
    status: Status = "met" if share <= TARGET_LLM_SHARE_MAX else "missed"
    if synthetic_llm(report):
        measured += "; LLM replayed from hand-authored fixtures (missing fixtures fall back to rules)"
        status = "caveat" if status == "met" else status
    return Row("Cost", label, value, measured, status, _src(*sources))


def usage_items(usage: Mapping[str, Any] | None, path: Path) -> list[dict[str, Any]]:
    if usage is None:
        return []
    items = usage.get("items")
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        raise InputError(f"{path}: `items` must be a list of objects")
    return list(items)


def tokens_row(inputs: Inputs) -> Row:
    label, value = "LLM tokens per LLM-path query", f"≤ {TARGET_TOKENS_PER_QUERY / 1000:g}k"
    for item in usage_items(inputs.usage, inputs.usage_path):
        requests = _num(item.get("requests"))
        if item.get("unit") == SEARCH_UNIT and requests:
            tokens = (
                (_num(item.get("input_tokens")) or 0.0) + (_num(item.get("output_tokens")) or 0.0)
            ) / requests
            status: Status = "met" if tokens <= TARGET_TOKENS_PER_QUERY else "missed"
            measured = f"{tokens:,.0f} (input + output, {requests:,.0f} requests, {item.get('model')})"
            return Row("Cost", label, value, measured, status, _src(inputs.usage_path.as_posix()))
    report = inputs.full
    llm = report.data.get("llm") if report else None
    avg = _num(llm.get("avg_tokens")) if isinstance(llm, dict) and llm.get("enabled") else None
    if report is None or not avg:
        return _unmeasured("Cost", label, value, _src(inputs.usage_path.as_posix()), "no LLM usage recorded")
    counts = llm_query_count(report)
    measured = f"{avg:,.0f}"
    if counts:
        measured += f" (eval average over {counts[0]} LLM-path {'query' if counts[0] == 1 else 'queries'})"
    status = "met" if avg <= TARGET_TOKENS_PER_QUERY else "missed"
    if synthetic_llm(report):
        measured += "; token counts from hand-authored fixtures"
        status = "caveat" if status == "met" else status
    return Row("Cost", label, value, measured, status, _src(_rel(inputs, report.path)))


def freshness_row(p95_s: float | None, source: str | None) -> Row:
    label, value = "Index freshness p95 (feed commit → searchable)", f"≤ {TARGET_FRESHNESS_P95_S:g} s"
    if p95_s is None:
        return _unmeasured("Freshness", label, value, "manual entry", "pass --freshness-p95-s")
    status: Status = "met" if p95_s <= TARGET_FRESHNESS_P95_S else "missed"
    return Row("Freshness", label, value, f"{p95_s:g} s", status, cell(source or "manual entry"))


def target_rows(
    inputs: Inputs,
    *,
    rps_override: float | None = None,
    llm_mode: str | None = None,
    reranker_variant: str = DEFAULT_RERANKER_VARIANT,
    freshness_p95_s: float | None = None,
    freshness_source: str | None = None,
) -> list[Row]:
    return [
        ndcg_lift_row(inputs, "en"),
        ndcg_lift_row(inputs, "ar"),
        recall_row(inputs),
        zero_result_row(inputs),
        filter_parse_row(inputs),
        standard_p95_row(inputs, rps_override),
        llm_p95_row(inputs, llm_mode),
        reranker_row(inputs, reranker_variant),
        llm_share_row(inputs),
        tokens_row(inputs),
        freshness_row(freshness_p95_s, freshness_source),
    ]


# ------------------------------------------------------------------------------------------------- cost


@dataclass(frozen=True)
class CostRow:
    unit: str
    model: str
    requests: float | None
    input_tokens: float
    output_tokens: float
    batch: bool
    usd: float | None
    note: str


def price_sheet_date(pricing: Mapping[str, Any] | None, text: str) -> str | None:
    for key in ("as_of", "price_sheet_date"):
        value = pricing.get(key) if pricing else None
        if value:
            return str(value)
    match = re.search(r"as of (\d{4}-\d{2}-\d{2})", text)
    return match.group(1) if match else None


def batch_multiplier(pricing: Mapping[str, Any] | None, model: str) -> tuple[float, bool]:
    """(multiplier, from_price_sheet). Model-level `batch_multiplier` wins over a top-level one."""
    models = pricing.get("models") if pricing else None
    entry = models.get(model) if isinstance(models, dict) else None
    for scope in (entry, pricing):
        value = _num(scope.get("batch_multiplier")) if isinstance(scope, dict) else None
        if value is not None:
            return value, True
    return DEFAULT_BATCH_MULTIPLIER, False


def price_usd(pricing: Mapping[str, Any] | None, item: Mapping[str, Any]) -> float | None:
    models = pricing.get("models") if pricing else None
    entry = models.get(str(item.get("model"))) if isinstance(models, dict) else None
    if not isinstance(entry, dict):
        return None
    input_tokens = _num(item.get("input_tokens")) or 0.0
    output_tokens = _num(item.get("output_tokens")) or 0.0
    requests = _num(item.get("requests"))
    in_rate, out_rate = _num(entry.get("input_per_mtok")), _num(entry.get("output_per_mtok"))
    if in_rate is None or out_rate is None:
        return None
    threshold = _num(entry.get("long_context_threshold_tokens"))
    # Long-context rates apply to a whole request whose prompt exceeds the threshold.
    if threshold is not None and requests and input_tokens / requests > threshold:
        in_rate = _num(entry.get("long_context_input_per_mtok")) or in_rate
        out_rate = _num(entry.get("long_context_output_per_mtok")) or out_rate
    usd = (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000
    if item.get("batch") is True:
        usd *= batch_multiplier(pricing, str(item.get("model")))[0]
    return usd


def cost_rows(inputs: Inputs) -> list[CostRow]:
    rows = []
    for item in usage_items(inputs.usage, inputs.usage_path):
        unit = str(item.get("unit", ""))
        note = str(item.get("note") or "")
        if unit not in USAGE_UNITS:
            note = f"unknown unit; {note}" if note else "unknown unit"
        rows.append(
            CostRow(
                unit=unit,
                model=str(item.get("model", "")),
                requests=_num(item.get("requests")),
                input_tokens=_num(item.get("input_tokens")) or 0.0,
                output_tokens=_num(item.get("output_tokens")) or 0.0,
                batch=item.get("batch") is True,
                usd=price_usd(inputs.pricing, item),
                note=note,
            )
        )
    return rows


# ---------------------------------------------------------------------------------------------- render


def _stamp_sources(inputs: Inputs) -> list[str]:
    parts = []
    for label, report in (("eval full", inputs.full), ("eval offline", inputs.offline)):
        if report:
            parts.append(f"{label} `{report.run_id}` ({_fmt_time(report.created_at)})")
        else:
            parts.append(f"{label}: none")
    parts.append(f"k6 run {_fmt_time(k6_started_at(inputs.k6))}" if inputs.k6 else "k6: none")
    parts.append("reranker bench (date not recorded)" if inputs.bench else "reranker bench: none")
    return parts


def render_targets(inputs: Inputs, rows: Sequence[Row], sha: str) -> str:
    lines = [
        f"_Generated by `ci/readme_report.py` at commit `{sha}` from: {'; '.join(_stamp_sources(inputs))}._",
        "",
        "| Area | Target | Value | Measured | Status | Source |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        cells = [r.area, r.target, r.value, cell(r.measured), STATUS_ICON[r.status], r.source]
        lines.append("| " + " | ".join(cells) + " |")
    lines += [
        "",
        "✅ met · ❌ missed · ⚠️ met only under the caveat in the Measured column, or not measured.",
    ]
    return "\n".join(lines) + "\n"


def render_cost(inputs: Inputs, rows: Sequence[CostRow], sha: str) -> str:
    usage_path = inputs.usage_path.as_posix()
    pricing_path = inputs.pricing_path.as_posix()
    generated = _fmt_time(parse_time(inputs.usage.get("generated_at"))) if inputs.usage else None
    sheet_date = price_sheet_date(inputs.pricing, inputs.pricing_text)
    usage_part = f"`{usage_path}` ({generated})" if generated else f"`{usage_path}`: none"
    pricing_part = (
        f"`{pricing_path}` (list prices as of {sheet_date or 'date not recorded'})"
        if inputs.pricing
        else f"`{pricing_path}`: none"
    )
    lines = [
        f"_Generated by `ci/readme_report.py` at commit `{sha}` from {usage_part} and {pricing_part}._",
        "",
    ]
    if not rows:
        lines.append(f"LLM usage per unit of work: {NOT_MEASURED} (no `{usage_path}`).")
        return "\n".join(lines) + "\n"
    lines += [
        "| Unit of work | Model | Requests | Input tokens | Output tokens | Batch | USD | Note |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        requests = f"{r.requests:,.0f}" if r.requests is not None else "–"
        usd = f"${r.usd:,.4f}" if r.usd is not None else "n/a (model not in the price sheet)"
        cells = [
            cell(r.unit),
            f"`{cell(r.model)}`",
            requests,
            f"{r.input_tokens:,.0f}",
            f"{r.output_tokens:,.0f}",
            "yes" if r.batch else "no",
            usd,
            cell(r.note),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    models = sorted({r.model for r in rows})
    sheet = inputs.pricing.get("models", {}) if inputs.pricing else {}
    for model in models:
        entry = sheet.get(model) if isinstance(sheet, dict) else None
        if not isinstance(entry, dict):
            continue
        multiplier, from_sheet = batch_multiplier(inputs.pricing, model)
        origin = "" if from_sheet else " (default: the price sheet has no batch_multiplier)"
        lines.append(
            f"- `{model}`: {_rate(entry.get('input_per_mtok'))} input / "
            f"{_rate(entry.get('output_per_mtok'))} output per million tokens; "
            f"batch requests × {multiplier:g}{origin}."
        )
    return "\n".join(lines) + "\n"


def _rate(value: Any) -> str:
    rate = _num(value)
    if rate is None:
        return "?"
    return f"${rate:.2f}" if round(rate, 2) == rate else f"${rate:g}"


def replace_block(text: str, name: str, content: str) -> str:
    start, end = f"<!-- report:{name}:start -->", f"<!-- report:{name}:end -->"
    if text.count(start) != 1 or text.count(end) != 1:
        raise InputError(f"expected exactly one {start} … {end} pair")
    i, j = text.index(start) + len(start), text.index(end)
    if j < i:
        raise InputError(f"{end} comes before {start}")
    return text[:i] + "\n" + content.rstrip("\n") + "\n" + text[j:]


# ------------------------------------------------------------------------------------------------- main


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0] if __doc__ else None)
    p.add_argument("--root", type=Path, default=REPO_ROOT, help="repo root; other paths are relative to it")
    p.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS_DIR)
    p.add_argument("--k6-summary", type=Path, default=DEFAULT_K6_SUMMARY)
    p.add_argument("--reranker-bench", type=Path, default=DEFAULT_RERANKER_BENCH)
    p.add_argument("--pricing", type=Path, default=DEFAULT_PRICING)
    p.add_argument("--usage", type=Path, default=DEFAULT_USAGE)
    p.add_argument("--reranker-variant", default=DEFAULT_RERANKER_VARIANT)
    p.add_argument("--k6-rps", type=float, default=None, help="offered rate of the k6 run (req/s)")
    p.add_argument("--k6-llm-mode", choices=["replay", "record", "live"], default=None)
    p.add_argument("--freshness-p95-s", type=float, default=None)
    p.add_argument("--freshness-source", default=None)
    p.add_argument("--sha", default=None, help="commit to stamp (default: git rev-parse --short HEAD)")
    p.add_argument("--write", type=Path, default=None, help="markdown file whose marker blocks to replace")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None, run: Runner = subprocess.run) -> int:
    args = parse_args(argv)
    root: Path = args.root
    try:
        inputs = load_inputs(
            root, args.reports_dir, args.k6_summary, args.reranker_bench, args.pricing, args.usage
        )
        sha = args.sha or git_short_sha(root, run)
        rows = target_rows(
            inputs,
            rps_override=args.k6_rps,
            llm_mode=args.k6_llm_mode,
            reranker_variant=args.reranker_variant,
            freshness_p95_s=args.freshness_p95_s,
            freshness_source=args.freshness_source,
        )
        blocks = {
            "targets": render_targets(inputs, rows, sha),
            "cost": render_cost(inputs, cost_rows(inputs), sha),
        }
        if args.write is None:
            for name in MARKERS:
                sys.stdout.write(
                    f"<!-- report:{name}:start -->\n{blocks[name]}<!-- report:{name}:end -->\n\n"
                )
            return 0
        target: Path = args.write if args.write.is_absolute() else root / args.write
        try:
            text = target.read_text(encoding="utf-8")
        except OSError as exc:
            raise InputError(f"{target}: {exc}") from exc
        updated = text
        for name in MARKERS:
            updated = replace_block(updated, name, blocks[name])
        if updated != text:
            target.write_text(updated, encoding="utf-8")
            print(f"readme_report: updated {target}", file=sys.stderr)
        else:
            print(f"readme_report: {target} already up to date", file=sys.stderr)
    except InputError as exc:
        print(f"readme_report: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

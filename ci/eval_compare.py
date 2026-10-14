#!/usr/bin/env python3
"""Compare a ranking-eval report against the main-branch baseline, render the PR comment, gate merges.

Reads the eval report schema from CLAUDE.md (``evals/reports/<run_id>.json``)::

    {"run_id", "git_sha", "mode": "offline|full",
     "slices": {"en"|"ar"|"arabizi_mixed"|"all": {"<ablation>": {"ndcg@10", "recall@100", "mrr",
                                                              "zero_result_rate", "n"}}},
     "filter_parse": {...}, "latency_ms": {...}, "llm": {...}}

Gate: nDCG@10 of the production ablation must not drop more than ``--max-drop-points`` (default 1.0
point = 0.01 nDCG) versus the baseline on any *language* slice (en, ar, arabizi_mixed). ``all`` is shown
but not gated: a regression confined to one language is exactly what the blended slice averages away.

The gated ablation is the most complete pipeline present in both reports (rrf_rerank_llm > rrf_rerank >
rrf), i.e. what actually serves traffic in that mode. Taking the max over ablations instead would let a
reranker regression hide behind a stable ``rrf`` row.

Exit codes: 0 = pass (or no baseline yet), 1 = regression, 2 = unusable input (missing/invalid report,
mode mismatch). Stdlib only so it runs on a bare runner without the uv environment.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

COMMENT_MARKER = "<!-- souq-eval-report -->"
LANGUAGE_SLICES: tuple[str, ...] = ("en", "ar", "arabizi_mixed")
DISPLAY_SLICES: tuple[str, ...] = (*LANGUAGE_SLICES, "all")
ABLATION_ORDER: tuple[str, ...] = ("bm25", "knn", "rrf", "rrf_rerank", "rrf_rerank_llm")
PRODUCTION_PRIORITY: tuple[str, ...] = ("rrf_rerank_llm", "rrf_rerank", "rrf")
GATE_METRIC = "ndcg@10"
DEFAULT_MAX_DROP_POINTS = 1.0
# (key, header, scale) — rank metrics are shown in points (x100) so deltas read in the gate's unit.
METRIC_COLUMNS: tuple[tuple[str, str, float], ...] = (
    ("ndcg@10", "nDCG@10", 100.0),
    ("recall@100", "Recall@100", 100.0),
    ("mrr", "MRR", 100.0),
    ("zero_result_rate", "Zero-result %", 100.0),
)
# Float noise from JSON round-trips must not turn a delta of exactly -1.0 into a regression.
_EPS = 1e-9

SliceStatus = Literal["ok", "regression", "no_baseline", "missing"]


class ReportError(ValueError):
    """The report file is missing, unreadable or does not match the eval report schema."""


@dataclass(frozen=True)
class SliceGate:
    slice: str
    baseline: float | None
    current: float | None
    delta_points: float | None
    status: SliceStatus


@dataclass(frozen=True)
class Comparison:
    gate_ablation: str | None
    max_drop_points: float
    has_baseline: bool
    slices: tuple[SliceGate, ...]
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def failed(self) -> bool:
        return any(s.status in ("regression", "missing") for s in self.slices)

    @property
    def regressions(self) -> tuple[SliceGate, ...]:
        return tuple(s for s in self.slices if s.status == "regression")


def load_report(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReportError(f"report not found: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportError(f"cannot read report {path}: {exc}") from exc
    validate_report(data, str(path))
    return data  # type: ignore[no-any-return]


def validate_report(data: object, origin: str = "report") -> None:
    if not isinstance(data, dict):
        raise ReportError(f"{origin}: top level must be a JSON object")
    slices = data.get("slices")
    if not isinstance(slices, dict) or not slices:
        raise ReportError(f"{origin}: 'slices' must be a non-empty object")
    for name, ablations in slices.items():
        if not isinstance(ablations, dict):
            raise ReportError(f"{origin}: slices.{name} must map ablation -> metrics")
        for ablation, metrics in ablations.items():
            if metrics is not None and not isinstance(metrics, dict):
                raise ReportError(f"{origin}: slices.{name}.{ablation} must be an object or null")


def metric(report: Mapping[str, Any], slice_name: str, ablation: str, key: str) -> float | None:
    """Return a finite metric value, or None when the slice/ablation/metric is absent or null."""
    ablations = report.get("slices", {}).get(slice_name)
    if not isinstance(ablations, Mapping):
        return None
    metrics = ablations.get(ablation)
    if not isinstance(metrics, Mapping):
        return None
    value = metrics.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def _has_ablation(report: Mapping[str, Any], ablation: str, slices: Sequence[str]) -> bool:
    return any(metric(report, s, ablation, GATE_METRIC) is not None for s in slices)


def choose_gate_ablation(
    current: Mapping[str, Any],
    baseline: Mapping[str, Any] | None,
    slices: Sequence[str] = LANGUAGE_SLICES,
    override: str | None = None,
) -> str | None:
    if override:
        return override
    for ablation in PRODUCTION_PRIORITY:
        if _has_ablation(current, ablation, slices) and (
            baseline is None or _has_ablation(baseline, ablation, slices)
        ):
            return ablation
    # Baseline predates the current pipeline: still gate the most complete ablation we produced, and
    # each slice will be reported as having no baseline rather than silently picking a weaker stage.
    for ablation in PRODUCTION_PRIORITY:
        if _has_ablation(current, ablation, slices):
            return ablation
    return None


def _delta_points(current: float, baseline: float) -> float:
    return round((current - baseline) * 100.0, 6)


def compare(
    current: Mapping[str, Any],
    baseline: Mapping[str, Any] | None,
    *,
    max_drop_points: float = DEFAULT_MAX_DROP_POINTS,
    gate_slices: Sequence[str] = LANGUAGE_SLICES,
    gate_ablation: str | None = None,
) -> Comparison:
    if baseline is not None and baseline.get("mode") != current.get("mode"):
        raise ReportError(
            f"baseline mode {baseline.get('mode')!r} != current mode {current.get('mode')!r}; "
            "offline and full evals are not comparable"
        )
    notes: list[str] = []
    ablation = choose_gate_ablation(current, baseline, gate_slices, gate_ablation)
    if ablation is None:
        notes.append(
            "No production ablation (" + ", ".join(PRODUCTION_PRIORITY) + ") found in the current report."
        )
        gates = tuple(SliceGate(s, None, None, None, "missing") for s in gate_slices)
        return Comparison(None, max_drop_points, baseline is not None, gates, tuple(notes))

    gates_list: list[SliceGate] = []
    for slice_name in gate_slices:
        cur = metric(current, slice_name, ablation, GATE_METRIC)
        base = metric(baseline, slice_name, ablation, GATE_METRIC) if baseline is not None else None
        if cur is None:
            # Fail closed: a slice that vanished from the report cannot prove it did not regress.
            gates_list.append(SliceGate(slice_name, base, None, None, "missing"))
            continue
        if base is None:
            gates_list.append(SliceGate(slice_name, None, cur, None, "no_baseline"))
            continue
        delta = _delta_points(cur, base)
        status: SliceStatus = "regression" if delta < -max_drop_points - _EPS else "ok"
        gates_list.append(SliceGate(slice_name, base, cur, delta, status))
        n_cur = metric(current, slice_name, ablation, "n")
        n_base = metric(baseline, slice_name, ablation, "n") if baseline is not None else None
        if n_cur is not None and n_base is not None and n_cur != n_base:
            notes.append(
                f"`{slice_name}` query count changed ({int(n_base)} -> {int(n_cur)}); "
                "part of the delta may come from the golden set, not the ranker."
            )
    return Comparison(ablation, max_drop_points, baseline is not None, tuple(gates_list), tuple(notes))


# --------------------------------------------------------------------------------------------- render


def _fmt_value(value: float | None, scale: float = 100.0) -> str:
    return "n/a" if value is None else f"{value * scale:.1f}"


def _fmt_delta(delta: float | None) -> str:
    if delta is None:
        return ""
    if abs(delta) < 0.05:
        return " (±0.0)"
    return f" ({delta:+.1f})"


def _cell(
    current: Mapping[str, Any],
    baseline: Mapping[str, Any] | None,
    slice_name: str,
    ablation: str,
    key: str,
    scale: float = 100.0,
) -> str:
    cur = metric(current, slice_name, ablation, key)
    if cur is None:
        return "—"
    base = metric(baseline, slice_name, ablation, key) if baseline is not None else None
    delta = None if base is None else round((cur - base) * scale, 6)
    return _fmt_value(cur, scale) + _fmt_delta(delta)


def _ablations_in(*reports: Mapping[str, Any] | None) -> list[str]:
    seen: set[str] = set()
    for report in reports:
        if report is None:
            continue
        for ablations in report.get("slices", {}).values():
            if isinstance(ablations, Mapping):
                seen.update(a for a, m in ablations.items() if isinstance(m, Mapping))
    known = [a for a in ABLATION_ORDER if a in seen]
    return known + sorted(seen - set(ABLATION_ORDER))


def _slices_in(report: Mapping[str, Any]) -> list[str]:
    present = list(report.get("slices", {}).keys())
    return [s for s in DISPLAY_SLICES if s in present] + sorted(set(present) - set(DISPLAY_SLICES))


def _short_sha(report: Mapping[str, Any] | None) -> str:
    sha = str((report or {}).get("git_sha") or "")
    return sha[:7] if sha else "unknown"


def _status_label(status: SliceStatus) -> str:
    return {
        "ok": "ok",
        "regression": "**REGRESSION**",
        "no_baseline": "no baseline",
        "missing": "**MISSING**",
    }[status]


def render_markdown(
    current: Mapping[str, Any],
    baseline: Mapping[str, Any] | None,
    comparison: Comparison,
    *,
    baseline_label: str | None = None,
    title: str = "Ranking eval",
) -> str:
    mode = current.get("mode", "?")
    verdict = "FAIL" if comparison.failed else "PASS"
    lines: list[str] = [COMMENT_MARKER, f"## {title} ({mode}) — {verdict}", ""]

    run = f"`{current.get('run_id', '?')}` @ `{_short_sha(current)}`"
    if baseline is None:
        lines += [
            f"Run {run}.",
            "",
            "**No baseline:** no eval report from `main` is available yet (first run, or the last main "
            "run expired/failed), so deltas are omitted and the regression gate is skipped for this run.",
            "",
        ]
    else:
        label = baseline_label or f"main @ `{_short_sha(baseline)}`"
        lines += [f"Run {run} vs baseline {label}. Deltas are in points (x100).", ""]

    if comparison.gate_ablation is not None:
        lines.append(
            f"Gate: nDCG@10 of `{comparison.gate_ablation}` may not drop more than "
            f"{comparison.max_drop_points:.1f} point on any language slice."
        )
    lines.append("")

    if baseline is not None or comparison.failed:
        lines += ["| slice | main | this PR | Δ (pts) | status |", "|---|---:|---:|---:|---|"]
        for g in comparison.slices:
            delta = "" if g.delta_points is None else f"{g.delta_points:+.1f}"
            lines.append(
                f"| {g.slice} | {_fmt_value(g.baseline)} | {_fmt_value(g.current)} | {delta} | "
                f"{_status_label(g.status)} |"
            )
        lines.append("")

    slices = _slices_in(current)
    ablations = _ablations_in(current, baseline)
    lines += ["### nDCG@10 by ablation", ""]
    lines.append("| ablation | " + " | ".join(slices) + " |")
    lines.append("|---|" + "---:|" * len(slices))
    for ablation in ablations:
        name = f"**{ablation}** (gated)" if ablation == comparison.gate_ablation else ablation
        cells = [_cell(current, baseline, s, ablation, GATE_METRIC) for s in slices]
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    lines.append("")

    for note in comparison.notes:
        lines.append(f"> {note}")
    if comparison.notes:
        lines.append("")

    lines += ["<details><summary>All metrics per slice</summary>", ""]
    for slice_name in slices:
        lines += [f"#### {slice_name}", ""]
        lines.append("| ablation | " + " | ".join(h for _, h, _ in METRIC_COLUMNS) + " | n |")
        lines.append("|---|" + "---:|" * (len(METRIC_COLUMNS) + 1))
        for ablation in ablations:
            if metric(current, slice_name, ablation, GATE_METRIC) is None and (
                metric(current, slice_name, ablation, "recall@100") is None
            ):
                continue
            cells = [_cell(current, baseline, slice_name, ablation, k, sc) for k, _, sc in METRIC_COLUMNS]
            n = metric(current, slice_name, ablation, "n")
            lines.append(f"| {ablation} | " + " | ".join(cells) + f" | {'' if n is None else int(n)} |")
        lines.append("")
    lines += _render_filter_parse(current, baseline)
    lines += _render_latency(current, baseline)
    lines += _render_llm(current)
    lines += ["</details>", ""]
    return "\n".join(lines)


def _num(obj: Any, *path: str) -> float | None:
    for key in path:
        if not isinstance(obj, Mapping):
            return None
        obj = obj.get(key)
    if isinstance(obj, bool) or not isinstance(obj, int | float) or not math.isfinite(obj):
        return None
    return float(obj)


def _render_filter_parse(current: Mapping[str, Any], baseline: Mapping[str, Any] | None) -> list[str]:
    fp = current.get("filter_parse")
    if not isinstance(fp, Mapping):
        return []

    def row(label: str, *path: str) -> str:
        cur = _num(current, "filter_parse", *path)
        base = _num(baseline, "filter_parse", *path) if baseline is not None else None
        delta = None if cur is None or base is None else round((cur - base) * 100.0, 6)
        return f"| {label} | {_fmt_value(cur)}{_fmt_delta(delta)} |"

    n = _num(current, "filter_parse", "n")
    out = ["#### Filter parsing", "", "| field | accuracy |", "|---|---:|"]
    out.append(row(f"exact match (n={'' if n is None else int(n)})", "exact_match"))
    per_field = fp.get("per_field")
    if isinstance(per_field, Mapping):
        out += [row(str(name), "per_field", str(name)) for name in sorted(per_field)]
    return [*out, ""]


def _fmt_ms(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}"


def _render_latency(current: Mapping[str, Any], baseline: Mapping[str, Any] | None) -> list[str]:
    lat = current.get("latency_ms")
    if not isinstance(lat, Mapping) or not lat:
        return []
    out = ["#### Stage latency (ms)", "", "| stage | p50 | p95 | p95 vs main |", "|---|---:|---:|---:|"]
    for stage in sorted(lat):
        p50 = _num(current, "latency_ms", stage, "p50")
        p95 = _num(current, "latency_ms", stage, "p95")
        base95 = _num(baseline, "latency_ms", stage, "p95") if baseline is not None else None
        diff = "" if p95 is None or base95 is None else f"{p95 - base95:+.1f}"
        out.append(f"| {stage} | {_fmt_ms(p50)} | {_fmt_ms(p95)} | {diff} |")
    return [*out, ""]


def _render_llm(current: Mapping[str, Any]) -> list[str]:
    llm = current.get("llm")
    if not isinstance(llm, Mapping):
        return []
    share = _num(llm, "path_share")
    tokens = _num(llm, "avg_tokens")
    usd = _num(llm, "usd_per_1k_queries")
    if share is None and tokens is None and usd is None:
        return []
    return [
        "#### LLM path",
        "",
        "| path share | avg tokens | USD / 1k queries |",
        "|---:|---:|---:|",
        f"| {'n/a' if share is None else f'{share * 100:.1f}%'} | "
        f"{'n/a' if tokens is None else f'{tokens:.0f}'} | {'n/a' if usd is None else f'{usd:.4f}'} |",
        "",
    ]


# ------------------------------------------------------------------------------------------------ cli


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    p.add_argument("--current", type=Path, required=True, help="eval report produced by this run")
    p.add_argument(
        "--baseline", type=Path, help="main-branch report; omitted or nonexistent path = no baseline"
    )
    p.add_argument("--markdown-out", type=Path, help="write the PR comment body here (default: stdout)")
    p.add_argument("--max-drop-points", type=float, default=DEFAULT_MAX_DROP_POINTS)
    p.add_argument("--gate-ablation", help="override the gated ablation (default: most complete pipeline)")
    p.add_argument("--baseline-label", help="how to describe the baseline in the comment (e.g. a run link)")
    p.add_argument("--title", default="Ranking eval")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        current = load_report(args.current)
        baseline = load_report(args.baseline) if args.baseline and args.baseline.exists() else None
        comparison = compare(
            current,
            baseline,
            max_drop_points=args.max_drop_points,
            gate_ablation=args.gate_ablation,
        )
    except ReportError as exc:
        print(f"eval_compare: {exc}", file=sys.stderr)
        return 2

    body = render_markdown(
        current, baseline, comparison, baseline_label=args.baseline_label, title=args.title
    )
    if args.markdown_out:
        args.markdown_out.write_text(body, encoding="utf-8")
    else:
        sys.stdout.write(body)

    for g in comparison.slices:
        if g.status == "regression":
            print(
                f"::error title=nDCG@10 regression::{g.slice}: {comparison.gate_ablation} "
                f"{_fmt_value(g.baseline)} -> {_fmt_value(g.current)} ({g.delta_points:+.1f} pts)",
                file=sys.stderr,
            )
        elif g.status == "missing":
            print(
                f"::error title=eval slice missing::{g.slice} has no nDCG@10 in the report", file=sys.stderr
            )
    if baseline is None:
        print("::notice title=eval baseline::no main-branch report found; gate skipped", file=sys.stderr)
    return 1 if comparison.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""CLI.

    python -m souq_evals [--offline | --full] [--out PATH] [--ablations ...] [--rebuild-index]
    python -m souq_evals label export --slice ar [--with-retrieved] [--out PATH]
    python -m souq_evals label import PATH
    python -m souq_evals judge --slice ar

Exit codes: 0 pass, 1 thresholds missed, 2 refused (judge gate) or setup error.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

from souq_evals.harness import FULL_ABLATIONS, OFFLINE_ABLATIONS, EvalConfig, Harness, git_sha


def _run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="souq_evals", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="no LLM path, deterministic (default)")
    mode.add_argument(
        "--full", action="store_true", help="adds rrf_rerank_llm (SOUQ_LLM_MODE decides replay/live)"
    )
    parser.add_argument(
        "--out", type=Path, default=None, help="report path (default evals/reports/<run_id>.json)"
    )
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--ablations", nargs="*", default=None)
    parser.add_argument("--index", default=os.environ.get("SOUQ_EVAL_INDEX", "souq_eval"))
    parser.add_argument(
        "--distractors", type=int, default=5000, help="random non-judged products in the corpus"
    )
    parser.add_argument("--rebuild-index", action="store_true")
    parser.add_argument(
        "--budget-scale", type=float, default=50.0, help="multiply stage budgets (quality runs)"
    )
    parser.add_argument("--no-thresholds", action="store_true", help="report only, never fail")
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="override a search.* config value, e.g. rerank_mode=rrf",
    )
    args = parser.parse_args(argv)
    full = bool(args.full)
    cfg = EvalConfig(
        mode="full" if full else "offline",
        ablations=args.ablations or (FULL_ABLATIONS if full else OFFLINE_ABLATIONS),
        index=args.index,
        distractors=args.distractors,
        rebuild_index=args.rebuild_index,
        budget_scale=args.budget_scale,
        overrides={k: yaml.safe_load(v) for k, v in (item.split("=", 1) for item in args.set)},
    )
    from souq_evals import report as rep

    run_id = args.run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + ("-full" if full else "-offline")
    results = asyncio.run(Harness(cfg).run())
    final = rep.finalise(run_id, git_sha(), cfg.mode, results)
    json_path, md_path = rep.write(final, args.out)
    print(md_path.read_text())
    print(f"report: {json_path}")
    if args.no_thresholds:
        return 0
    failures = rep.check_thresholds(final)
    for f in failures:
        print(f"THRESHOLD FAILED: {f}", file=sys.stderr)
    return 1 if failures else 0


def _label(argv: list[str]) -> int:
    from souq_evals import labelling
    from souq_evals.corpus import load_products

    parser = argparse.ArgumentParser(prog="souq_evals label")
    sub = parser.add_subparsers(dest="cmd", required=True)
    exp = sub.add_parser("export")
    exp.add_argument("--slice", default="ar")
    exp.add_argument("--out", type=Path, default=None)
    exp.add_argument("--with-retrieved", action="store_true", help="add the system's top-20 unjudged results")
    imp = sub.add_parser("import")
    imp.add_argument("path", type=Path)
    imp.add_argument("--source", choices=["maintainer", "claude"], default="maintainer")
    args = parser.parse_args(argv)
    if args.cmd == "import":
        print(labelling.import_csv(args.path, args.source))
        return 0
    retrieved = asyncio.run(_retrieved(args.slice)) if args.with_retrieved else None
    print(labelling.export_csv(args.slice, load_products(), args.out, retrieved))
    return 0


async def _retrieved(slice_name: str) -> dict[str, list[str]]:
    from souq_evals.golden import SLICE_LANG, load_golden
    from souq_evals.harness import ABLATIONS, ranking
    from souq_search.models import SearchRequest

    harness = Harness(EvalConfig())
    golden = load_golden()
    pipeline, _ = await harness.build_pipeline(golden)
    out = {}
    for q in golden:
        if q.slice == slice_name:
            resp = await pipeline.search(
                SearchRequest(q=q.q, lang=SLICE_LANG[q.slice], limit=20), ABLATIONS["rrf_rerank"]
            )
            out[q.id] = ranking(resp.debug or {}, "rrf_rerank")[:20]
    return out


def _judge(argv: list[str]) -> int:
    from souq_common.settings import get_settings
    from souq_evals.corpus import load_products
    from souq_evals.golden import load_golden
    from souq_evals.judge import LlmJudge, calibrate
    from souq_evals.labelling import reference_labels, stratified_sample, write_reference_csv
    from souq_evals.paths import LABELLING
    from souq_search.parsing.llm_fixtures import AnthropicCaller, LlmTransport

    parser = argparse.ArgumentParser(prog="souq_evals judge")
    parser.add_argument("--slice", default="ar")
    parser.add_argument(
        "--reference",
        choices=["maintainer", "claude"],
        default="maintainer",
        help="whose labels the judge must agree with; 'claude' measures model-model agreement, not human",
    )
    parser.add_argument(
        "--sample", type=int, default=0, help="calibrate on N reference pairs (stratified by grade, seeded)"
    )
    args = parser.parse_args(argv)
    s = get_settings()
    caller = (
        AnthropicCaller(s.anthropic_api_key) if s.souq_llm_mode != "replay" and s.anthropic_api_key else None
    )
    judge = LlmJudge(LlmTransport(s.souq_llm_mode, caller=caller), s.souq_llm_model)
    golden = load_golden()
    queries = {q.id: q.q for q in golden}
    reference = reference_labels(args.reference)
    if args.sample:
        reference = stratified_sample(reference, args.sample)
        write_reference_csv(reference, queries, LABELLING / f"kappa_reference_{len(reference)}.csv")
    result = asyncio.run(calibrate(judge, reference, queries, load_products()))
    print(
        f"judge gate (reference={args.reference}): allowed={result.allowed} kappa={result.kappa} "
        f"pairs={result.n_pairs} ({result.reason})"
    )
    if not result.allowed:
        print(f"refusing to label {args.slice} with the LLM judge", file=sys.stderr)
        return 2
    print("judge calibrated; label new queries with `python -m souq_evals label` + the judge output")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "label":
        return _label(argv[1:])
    if argv and argv[0] == "judge":
        return _judge(argv[1:])
    return _run(argv)


if __name__ == "__main__":
    raise SystemExit(main())

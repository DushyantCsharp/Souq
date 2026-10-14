from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ci import readme_report as rr

FIXTURES = Path(__file__).parent / "fixtures" / "readme"
FIXTURE_ARGS = [
    "--root",
    str(FIXTURES),
    "--reports-dir",
    "reports",
    "--k6-summary",
    "k6_summary.json",
    "--reranker-bench",
    "reranker_bench.json",
    "--pricing",
    "pricing.yaml",
    "--usage",
    "usage.json",
]


def _inputs(root: Path = FIXTURES, **overrides: Any) -> rr.Inputs:
    inputs = rr.load_inputs(
        root,
        Path("reports"),
        Path("k6_summary.json"),
        Path("reranker_bench.json"),
        Path("pricing.yaml"),
        Path("usage.json"),
    )
    return rr.Inputs(**{**inputs.__dict__, **overrides})


def _row(rows: list[rr.Row], target_prefix: str) -> rr.Row:
    return next(r for r in rows if r.target.startswith(target_prefix))


def _with_labels(report: rr.EvalReport, labels: dict[str, Any]) -> rr.EvalReport:
    data = copy.deepcopy(report.data)
    data["labels"] = labels
    return rr.EvalReport(report.path, data, report.created_at)


class TestEvalReports:
    def test_newest_report_per_mode_wins_and_non_reports_are_skipped(self) -> None:
        reports = rr.latest_eval_reports(FIXTURES / "reports")
        assert set(reports) == {"full", "offline"}
        assert reports["offline"].run_id == "fixture-offline-new"
        assert reports["full"].run_id == "fixture-full"

    def test_missing_dir_is_empty(self, tmp_path: Path) -> None:
        assert rr.latest_eval_reports(tmp_path / "nope") == {}

    def test_malformed_report_is_an_input_error(self, tmp_path: Path) -> None:
        (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(rr.InputError):
            rr.latest_eval_reports(tmp_path)

    def test_production_ablation_priority(self) -> None:
        assert rr.production_ablation({"bm25": {}, "rrf": {}, "rrf_rerank": {}}) == "rrf_rerank"
        assert rr.production_ablation({"bm25": {}, "rrf_rerank_llm": {}, "rrf": {}}) == "rrf_rerank_llm"
        assert rr.production_ablation({"bm25": {}}) is None


class TestRelevanceRows:
    def test_english_lift_below_target_is_a_miss(self) -> None:
        row = rr.ndcg_lift_row(_inputs(), "en")
        assert row.status == "missed"
        assert row.measured.startswith("+2.8 points (rrf_rerank_llm 0.587 vs bm25 0.559)")
        assert row.source == "`reports/full.json`"

    def test_arabic_lift_met_on_provisional_labels_is_a_caveat(self) -> None:
        row = rr.ndcg_lift_row(_inputs(), "ar")
        assert row.status == "caveat"
        assert "+26.1 points" in row.measured
        assert "100 % of judgments not human-labelled (provisional 1,215)" in row.measured

    def test_arabic_lift_on_human_labels_is_met(self) -> None:
        inputs = _inputs()
        assert inputs.full is not None
        full = _with_labels(inputs.full, {"ar": {"maintainer": 1215, "claude_share": 0.0}})
        row = rr.ndcg_lift_row(_inputs(full=full), "ar")
        assert row.status == "met"
        assert "human" not in row.measured

    def test_claude_labels_count_as_not_human(self) -> None:
        inputs = _inputs()
        assert inputs.full is not None
        full = _with_labels(inputs.full, {"ar": {"claude": 900, "esci": 100, "claude_share": 0.9}})
        share, counts = rr.non_human_label_share(full, ["ar"])
        assert share == pytest.approx(0.9)
        assert counts == {"claude": 900}

    def test_offline_report_is_used_without_a_full_one(self) -> None:
        row = rr.ndcg_lift_row(_inputs(full=None), "en")
        assert "rrf_rerank " in row.measured
        assert row.source == "`reports/offline_new.json`"

    def test_recall_and_zero_result(self) -> None:
        inputs = _inputs()
        recall = rr.recall_row(inputs)
        assert recall.status == "missed"
        assert recall.measured.startswith("0.646 (rrf_rerank_llm)")
        zero = rr.zero_result_row(inputs)
        assert zero.status == "met"
        assert zero.measured == "0.0 % (rrf_rerank_llm)"  # label-independent: no label caveat

    def test_no_reports_means_not_measured(self, tmp_path: Path) -> None:
        inputs = _inputs(tmp_path)
        for row in (rr.ndcg_lift_row(inputs, "en"), rr.recall_row(inputs), rr.filter_parse_row(inputs)):
            assert row.status == "unmeasured"
            assert row.measured.startswith(rr.NOT_MEASURED)


class TestFilterParse:
    def test_full_report_with_synthetic_llm_is_a_caveat(self) -> None:
        row = rr.filter_parse_row(_inputs())
        assert row.status == "caveat"
        assert "100.0 % (rule+llm, n=80); rule parser alone 90.0 %" in row.measured
        assert "hand-authored" in row.measured

    def test_rule_only_offline_score_below_target_is_a_miss(self) -> None:
        row = rr.filter_parse_row(_inputs(full=None))
        assert row.status == "missed"
        assert row.measured == "90.0 % (rule, n=80)"

    def test_recorded_llm_fixtures_are_not_a_caveat(self) -> None:
        inputs = _inputs()
        assert inputs.full is not None
        data = copy.deepcopy(inputs.full.data)
        data["llm"]["fixture_provenance"] = {"api": 1}
        full = rr.EvalReport(inputs.full.path, data, inputs.full.created_at)
        assert rr.filter_parse_row(_inputs(full=full)).status == "met"


class TestK6:
    def test_offered_rate_from_override_setup_data_or_threshold(self) -> None:
        summary: dict[str, Any] = json.loads((FIXTURES / "k6_summary.json").read_text(encoding="utf-8"))
        assert rr.k6_offered_rps(summary) == 200.0  # dropped_iterations: rate<2  =>  RATE = 200
        assert rr.k6_offered_rps(summary, override=60.0) == 60.0
        summary["setup_data"]["rate"] = 150
        assert rr.k6_offered_rps(summary) == 150.0
        del summary["setup_data"]["rate"]
        summary["metrics"]["dropped_iterations"]["thresholds"] = {"rate<1": {"ok": True}}
        assert rr.k6_offered_rps(summary) is None  # only says RATE <= 100

    def test_overload_at_target_rate_is_a_miss(self) -> None:
        row = rr.standard_p95_row(_inputs(), None)
        assert row.status == "missed"
        assert row.measured == "4,615 ms (200 RPS offered, 152.4 achieved); errors 99.9 %"

    def _k6(self, p95: float, errors: float) -> dict[str, Any]:
        summary: dict[str, Any] = json.loads((FIXTURES / "k6_summary.json").read_text(encoding="utf-8"))
        summary["metrics"]["search_latency_standard"]["values"]["p(95)"] = p95
        summary["metrics"]["http_req_failed"]["values"]["rate"] = errors
        return summary

    def test_met_at_target_rate(self) -> None:
        assert rr.standard_p95_row(_inputs(k6=self._k6(180.0, 0.002)), None).status == "met"

    def test_met_below_target_rate_is_only_a_caveat(self) -> None:
        row = rr.standard_p95_row(_inputs(k6=self._k6(180.0, 0.002)), 60.0)
        assert row.status == "caveat"
        assert "60 RPS offered" in row.measured

    def test_fast_but_failing_is_a_miss(self) -> None:
        assert rr.standard_p95_row(_inputs(k6=self._k6(40.0, 0.3)), None).status == "missed"

    def test_llm_p95_without_llm_traffic_is_not_a_measurement(self) -> None:
        summary = self._k6(100.0, 0.0)
        summary["metrics"]["search_latency_llm"]["values"]["p(95)"] = 900.0
        row = rr.llm_p95_row(_inputs(k6=summary), None)
        assert row.status == "caveat"
        assert "no request took the LLM path" in row.measured

    def test_llm_p95_needs_live_mode(self) -> None:
        summary = self._k6(100.0, 0.0)
        summary["metrics"]["search_latency_llm"]["values"]["p(95)"] = 900.0
        summary["metrics"]["llm_path_share"]["values"]["rate"] = 0.08
        assert rr.llm_p95_row(_inputs(k6=summary), None).status == "caveat"
        assert rr.llm_p95_row(_inputs(k6=summary), "live").status == "met"
        summary["metrics"]["search_latency_llm"]["values"]["p(95)"] = 2500.0
        assert rr.llm_p95_row(_inputs(k6=summary), "live").status == "missed"

    def test_missing_summary_is_not_measured(self, tmp_path: Path) -> None:
        inputs = _inputs(tmp_path)
        assert rr.standard_p95_row(inputs, None).status == "unmeasured"
        assert rr.llm_p95_row(inputs, None).status == "unmeasured"


class TestRerankerAndLlmRows:
    def test_quiet_cpu_meets_but_loaded_misses(self) -> None:
        row = rr.reranker_row(_inputs(), rr.DEFAULT_RERANKER_VARIANT)
        assert row.status == "caveat"
        assert row.measured == (
            "105.1 ms (mmarco-mMiniLMv2-L12-H384-v1-int8, load avg 1.9); 561.6 ms at load avg 28.5"
        )

    def test_slow_variant_is_a_miss(self) -> None:
        row = rr.reranker_row(_inputs(), "bge-reranker-v2-m3-int8@len128/t4/c8quiet")
        assert row.status == "missed"

    def test_unknown_variant_is_not_measured(self) -> None:
        assert rr.reranker_row(_inputs(), "nope").status == "unmeasured"

    def test_llm_share_with_synthetic_fixtures_is_a_caveat(self) -> None:
        row = rr.llm_share_row(_inputs())
        assert row.status == "caveat"
        assert row.measured.startswith("0.7 % (1 of 150 golden-set queries); k6 traffic mix 0.0 %")

    def test_tokens_prefer_live_usage(self) -> None:
        row = rr.tokens_row(_inputs())
        assert row.status == "met"
        assert row.measured.startswith("510 (input + output, 100 requests")  # (45,000 + 6,000) / 100
        assert row.source == "`usage.json`"

    def test_tokens_fall_back_to_eval_average(self) -> None:
        row = rr.tokens_row(_inputs(usage=None))
        assert row.status == "caveat"
        assert row.measured.startswith("450 (eval average over 1 LLM-path query)")

    def test_freshness_is_manual(self) -> None:
        assert rr.freshness_row(None, None).status == "unmeasured"
        row = rr.freshness_row(8.3, "docs/handoffs/workers-STATUS.md")
        assert (row.status, row.measured) == ("met", "8.3 s")
        assert rr.freshness_row(31.0, None).status == "missed"


class TestCost:
    def test_usd_per_unit_with_batch_discount(self) -> None:
        rows = {r.unit: r for r in rr.cost_rows(_inputs())}
        # (45,000 x 0.10 + 6,000 x 0.50) / 1e6
        assert rows["1k searches"].usd == pytest.approx(0.0075)
        # (120,000 x 0.10 + 30,000 x 0.50) / 1e6 x 0.5 (default batch multiplier)
        assert rows["1k titles translated"].usd == pytest.approx(0.0135)
        # (400,000 x 2.00 + 20,000 x 10.00) / 1e6
        assert rows["1k judged pairs"].usd == pytest.approx(1.0)

    def test_batch_multiplier_from_price_sheet_wins(self) -> None:
        pricing: dict[str, Any] = {
            "batch_multiplier": 0.4,
            "models": {"m": {"input_per_mtok": 1.0, "output_per_mtok": 1.0}},
        }
        assert rr.batch_multiplier(pricing, "m") == (0.4, True)
        pricing["models"]["m"]["batch_multiplier"] = 0.3
        assert rr.batch_multiplier(pricing, "m") == (0.3, True)
        assert rr.batch_multiplier({}, "m") == (rr.DEFAULT_BATCH_MULTIPLIER, False)

    def test_long_context_rates_apply_per_request(self) -> None:
        pricing, _ = rr.load_yaml(FIXTURES / "pricing.yaml")
        item = {"model": "claude-haiku-5-5", "input_tokens": 200_000, "output_tokens": 1_000, "requests": 1}
        # 200k input tokens in one request > 100k threshold: 200,000 x 0.50 + 1,000 x 2.50
        assert rr.price_usd(pricing, item) == pytest.approx(0.1025)

    def test_unknown_model_has_no_price(self) -> None:
        pricing, _ = rr.load_yaml(FIXTURES / "pricing.yaml")
        assert rr.price_usd(pricing, {"model": "gpt-x", "input_tokens": 1, "output_tokens": 1}) is None

    def test_price_sheet_date(self) -> None:
        pricing, text = rr.load_yaml(FIXTURES / "pricing.yaml")
        assert rr.price_sheet_date(pricing, text) == "2026-10-06"
        assert rr.price_sheet_date({"as_of": "2027-01-01"}, text) == "2027-01-01"
        assert rr.price_sheet_date({}, "") is None

    def test_items_must_be_a_list(self) -> None:
        with pytest.raises(rr.InputError):
            rr.usage_items({"items": {"unit": "1k searches"}}, Path("usage.json"))

    def test_render_cost(self) -> None:
        inputs = _inputs()
        text = rr.render_cost(inputs, rr.cost_rows(inputs), "abc1234")
        assert "commit `abc1234`" in text
        assert "(list prices as of 2026-10-06)" in text
        assert (
            "| 1k titles translated | `claude-haiku-5-5` | 1,000 | 120,000 | 30,000 | yes | $0.0135 |" in text
        )
        assert "$0.10 input / $0.50 output per million tokens; batch requests × 0.5 (default" in text

    def test_render_cost_without_usage(self) -> None:
        inputs = _inputs(usage=None)
        text = rr.render_cost(inputs, rr.cost_rows(inputs), "abc1234")
        assert "not measured" in text


class TestRenderAndWrite:
    def test_cell_escapes_pipes(self) -> None:
        assert rr.cell("a | b\nc") == "a \\| b c"

    def test_targets_block_is_stamped(self) -> None:
        inputs = _inputs()
        text = rr.render_targets(inputs, rr.target_rows(inputs), "abc1234")
        assert "commit `abc1234`" in text
        assert "eval full `fixture-full` (2026-10-14 05:02 UTC)" in text
        assert "eval offline `fixture-offline-new` (2026-10-14 05:01 UTC)" in text
        assert "k6 run 2026-10-14 05:52 UTC" in text
        assert text.count("\n| ") == 12  # header + 11 target rows

    def test_replace_block_keeps_markers_and_surrounding_text(self) -> None:
        text = (FIXTURES / "README.md").read_text(encoding="utf-8")
        out = rr.replace_block(text, "targets", "NEW\n")
        assert "<!-- report:targets:start -->\nNEW\n<!-- report:targets:end -->" in out
        assert "stale targets table" not in out
        assert "stale cost table" in out
        assert out.endswith("Trailing text stays.\n")

    def test_replace_block_requires_exactly_one_pair(self) -> None:
        with pytest.raises(rr.InputError):
            rr.replace_block("no markers", "targets", "x")
        twice = "<!-- report:cost:start --><!-- report:cost:end -->" * 2
        with pytest.raises(rr.InputError):
            rr.replace_block(twice, "cost", "x")

    def test_main_writes_both_blocks_and_is_idempotent(self, tmp_path: Path) -> None:
        readme = tmp_path / "README.md"
        shutil.copy(FIXTURES / "README.md", readme)
        args = [*FIXTURE_ARGS, "--sha", "abc1234", "--write", str(readme), "--freshness-p95-s", "8.3"]
        assert rr.main(args) == 0
        first = readme.read_text(encoding="utf-8")
        assert "stale" not in first
        assert "| Freshness | Index freshness p95" in first
        assert "| 1k searches |" in first
        assert rr.main(args) == 0
        assert readme.read_text(encoding="utf-8") == first

    def test_main_prints_blocks_with_markers(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert rr.main([*FIXTURE_ARGS, "--sha", "abc1234"]) == 0
        out = capsys.readouterr().out
        for name in rr.MARKERS:
            assert f"<!-- report:{name}:start -->" in out
            assert f"<!-- report:{name}:end -->" in out

    def test_main_with_no_inputs_renders_not_measured(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert rr.main(["--root", str(tmp_path), "--sha", "abc1234"]) == 0
        out = capsys.readouterr().out
        assert out.count(rr.NOT_MEASURED) >= 11

    def test_main_exits_2_on_malformed_input_or_missing_markers(self, tmp_path: Path) -> None:
        (tmp_path / "bad.json").write_text("[", encoding="utf-8")
        assert rr.main(["--root", str(tmp_path), "--k6-summary", "bad.json", "--sha", "x"]) == 2
        (tmp_path / "plain.md").write_text("# no markers\n", encoding="utf-8")
        assert rr.main([*FIXTURE_ARGS, "--sha", "x", "--write", str(tmp_path / "plain.md")]) == 2

    def test_sha_comes_from_git_when_not_given(self, capsys: pytest.CaptureFixture[str]) -> None:
        def fake_git(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
            assert cmd == ["git", "rev-parse", "--short", "HEAD"]
            return subprocess.CompletedProcess(cmd, 0, stdout="f00ba12\n", stderr="")

        assert rr.main(FIXTURE_ARGS, run=fake_git) == 0
        assert "commit `f00ba12`" in capsys.readouterr().out

    def test_git_failure_stamps_unknown(self) -> None:
        def no_git(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
            raise subprocess.CalledProcessError(128, cmd)

        assert rr.git_short_sha(FIXTURES, run=no_git) == "unknown"

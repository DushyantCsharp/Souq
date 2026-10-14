from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from ci import eval_compare as ec

FIXTURES = Path(__file__).parent / "fixtures" / "eval"


def _load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return data


@pytest.fixture
def main_report() -> dict[str, Any]:
    return _load("main_offline")


@pytest.fixture
def pr_ok() -> dict[str, Any]:
    return _load("pr_offline_ok")


@pytest.fixture
def pr_regressed() -> dict[str, Any]:
    return _load("pr_offline_regressed")


def _set_ndcg(report: dict[str, Any], slice_name: str, ablation: str, value: float) -> dict[str, Any]:
    out = copy.deepcopy(report)
    out["slices"][slice_name][ablation]["ndcg@10"] = value
    return out


class TestGateAblation:
    def test_offline_reports_gate_rrf_rerank(
        self, main_report: dict[str, Any], pr_ok: dict[str, Any]
    ) -> None:
        assert ec.choose_gate_ablation(pr_ok, main_report) == "rrf_rerank"

    def test_full_reports_gate_llm_pipeline(self) -> None:
        full = _load("nightly_full")
        assert ec.choose_gate_ablation(full, full) == "rrf_rerank_llm"

    def test_falls_back_to_pipeline_present_in_both(self, main_report: dict[str, Any]) -> None:
        # Current has the LLM ablation but the baseline does not: compare like with like.
        full_like = copy.deepcopy(_load("nightly_full"))
        full_like["mode"] = "offline"
        assert ec.choose_gate_ablation(full_like, main_report) == "rrf_rerank"

    def test_override_wins(self, pr_ok: dict[str, Any]) -> None:
        assert ec.choose_gate_ablation(pr_ok, None, override="bm25") == "bm25"


class TestCompare:
    def test_drop_of_exactly_one_point_passes(
        self, main_report: dict[str, Any], pr_ok: dict[str, Any]
    ) -> None:
        result = ec.compare(pr_ok, main_report)
        en = next(g for g in result.slices if g.slice == "en")
        assert en.delta_points == pytest.approx(-1.0)
        assert en.status == "ok"
        assert not result.failed

    def test_drop_over_one_point_on_language_slice_fails(
        self, main_report: dict[str, Any], pr_regressed: dict[str, Any]
    ) -> None:
        result = ec.compare(pr_regressed, main_report)
        assert result.failed
        assert [g.slice for g in result.regressions] == ["ar"]
        assert result.regressions[0].delta_points == pytest.approx(-1.4)

    def test_ungated_ablation_improvement_does_not_mask_regression(
        self, main_report: dict[str, Any], pr_regressed: dict[str, Any]
    ) -> None:
        # rrf improves on ar in this fixture; the gate must still look at the production pipeline.
        pr_rrf = ec.metric(pr_regressed, "ar", "rrf", "ndcg@10")
        main_rrf = ec.metric(main_report, "ar", "rrf", "ndcg@10")
        assert pr_rrf is not None
        assert main_rrf is not None
        assert pr_rrf > main_rrf
        assert ec.compare(pr_regressed, main_report).failed

    def test_all_slice_is_not_gated(self, main_report: dict[str, Any], pr_ok: dict[str, Any]) -> None:
        current = _set_ndcg(pr_ok, "all", "rrf_rerank", 0.40)
        result = ec.compare(current, main_report)
        assert not result.failed
        assert {g.slice for g in result.slices} == {"en", "ar", "arabizi_mixed"}

    def test_no_baseline_passes_and_says_so(self, pr_ok: dict[str, Any]) -> None:
        result = ec.compare(pr_ok, None)
        assert not result.failed
        assert not result.has_baseline
        assert all(g.status == "no_baseline" for g in result.slices)

    def test_missing_language_slice_fails_closed(
        self, main_report: dict[str, Any], pr_ok: dict[str, Any]
    ) -> None:
        current = copy.deepcopy(pr_ok)
        del current["slices"]["arabizi_mixed"]
        result = ec.compare(current, main_report)
        assert result.failed
        assert next(g for g in result.slices if g.slice == "arabizi_mixed").status == "missing"

    def test_slice_new_in_pr_has_no_baseline(
        self, main_report: dict[str, Any], pr_ok: dict[str, Any]
    ) -> None:
        baseline = copy.deepcopy(main_report)
        del baseline["slices"]["arabizi_mixed"]
        result = ec.compare(pr_ok, baseline)
        assert not result.failed
        assert next(g for g in result.slices if g.slice == "arabizi_mixed").status == "no_baseline"

    def test_null_metric_counts_as_missing(self, main_report: dict[str, Any], pr_ok: dict[str, Any]) -> None:
        current = copy.deepcopy(pr_ok)
        current["slices"]["en"]["rrf_rerank"]["ndcg@10"] = None
        assert ec.compare(current, main_report).failed

    def test_mode_mismatch_is_an_input_error(self, pr_ok: dict[str, Any]) -> None:
        with pytest.raises(ec.ReportError, match="not comparable"):
            ec.compare(pr_ok, _load("nightly_full"))

    def test_custom_threshold(self, main_report: dict[str, Any], pr_ok: dict[str, Any]) -> None:
        assert ec.compare(pr_ok, main_report, max_drop_points=0.5).failed

    def test_golden_set_size_change_is_noted(
        self, main_report: dict[str, Any], pr_ok: dict[str, Any]
    ) -> None:
        current = copy.deepcopy(pr_ok)
        current["slices"]["ar"]["rrf_rerank"]["n"] = 64
        notes = ec.compare(current, main_report).notes
        assert any("`ar` query count changed (60 -> 64)" in n for n in notes)


class TestValidation:
    @pytest.mark.parametrize(
        "payload",
        [[], {"run_id": "x"}, {"slices": {}}, {"slices": {"en": []}}, {"slices": {"en": {"bm25": 3}}}],
    )
    def test_rejects_malformed_reports(self, payload: object) -> None:
        with pytest.raises(ec.ReportError):
            ec.validate_report(payload)

    def test_load_report_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ec.ReportError, match="not found"):
            ec.load_report(tmp_path / "nope.json")


class TestRender:
    def test_comment_has_marker_table_and_deltas(
        self, main_report: dict[str, Any], pr_regressed: dict[str, Any]
    ) -> None:
        body = ec.render_markdown(pr_regressed, main_report, ec.compare(pr_regressed, main_report))
        assert body.startswith(ec.COMMENT_MARKER)
        assert "— FAIL" in body
        assert "| ar | 54.4 | 53.0 | -1.4 | **REGRESSION** |" in body
        # ablation table row with per-slice delta in points
        assert "| **rrf_rerank** (gated) | 62.5 (+0.2) | 53.0 (-1.4) | 43.3 (±0.0) | 54.3 (-0.4) |" in body
        assert "main @ `1111111`" in body

    def test_first_run_without_baseline(self, pr_ok: dict[str, Any]) -> None:
        body = ec.render_markdown(pr_ok, None, ec.compare(pr_ok, None))
        assert "— PASS" in body
        assert "**No baseline:**" in body
        assert "(+" not in body
        assert "(-" not in body
        assert "| bm25 | 51.2 | 40.5 | 21.2 | 39.3 |" in body

    def test_full_report_sections(self) -> None:
        full = _load("nightly_full")
        body = ec.render_markdown(full, full, ec.compare(full, full), title="Nightly eval")
        assert "## Nightly eval (full) — PASS" in body
        assert "#### LLM path" in body
        assert "| 18.0% | 412 | 0.0731 |" in body
        assert "#### Filter parsing" in body
        assert "| price | 95.0 (±0.0) |" in body
        assert "| rerank | 41.0 | 88.4 | +0.0 |" in body

    def test_ablation_absent_in_slice_renders_dash(self, main_report: dict[str, Any]) -> None:
        full = copy.deepcopy(_load("nightly_full"))
        full["mode"] = "offline"
        del full["slices"]["ar"]["rrf_rerank_llm"]
        body = ec.render_markdown(full, main_report, ec.compare(full, main_report))
        row = next(line for line in body.splitlines() if line.startswith("| rrf_rerank_llm |"))
        assert row.split("|")[2].strip() == "64.1"
        assert row.split("|")[3].strip() == "—"


class TestCli:
    def test_regression_exit_code_and_markdown_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = tmp_path / "comment.md"
        code = ec.main(
            [
                "--current",
                str(FIXTURES / "pr_offline_regressed.json"),
                "--baseline",
                str(FIXTURES / "main_offline.json"),
                "--markdown-out",
                str(out),
            ]
        )
        assert code == 1
        assert out.read_text(encoding="utf-8").startswith(ec.COMMENT_MARKER)
        assert "::error title=nDCG@10 regression::ar" in capsys.readouterr().err

    def test_pass(self) -> None:
        argv = [
            "--current",
            str(FIXTURES / "pr_offline_ok.json"),
            "--baseline",
            str(FIXTURES / "main_offline.json"),
        ]
        assert ec.main(argv) == 0

    def test_nonexistent_baseline_path_means_first_run(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        argv = [
            "--current",
            str(FIXTURES / "pr_offline_ok.json"),
            "--baseline",
            str(tmp_path / "absent.json"),
        ]
        assert ec.main(argv) == 0
        captured = capsys.readouterr()
        assert "**No baseline:**" in captured.out
        assert "gate skipped" in captured.err

    def test_invalid_current_report_exits_2(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        assert ec.main(["--current", str(bad)]) == 2

    def test_mode_mismatch_exits_2(self) -> None:
        argv = [
            "--current",
            str(FIXTURES / "pr_offline_ok.json"),
            "--baseline",
            str(FIXTURES / "nightly_full.json"),
        ]
        assert ec.main(argv) == 2

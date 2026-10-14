from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ci import check_image_size as cis

REPO_BUDGETS = Path(__file__).resolve().parents[1] / "image-budgets.json"


@pytest.fixture
def budgets(tmp_path: Path) -> Path:
    path = tmp_path / "budgets.json"
    path.write_text(json.dumps({"images": {"search": {"max_mib": 100}, "gateway": {"max_mib": 50.5}}}))
    return path


def test_repo_budgets_file_is_valid_and_covers_search() -> None:
    loaded = cis.load_budgets(REPO_BUDGETS)
    assert "search" in loaded
    assert set(loaded) >= {"gateway", "storefront", "search", "indexer", "feeds", "outbox-relay"}


def test_within_budget(budgets: Path) -> None:
    assert cis.main(["--name", "search", "--size-bytes", str(100 * cis.MIB), "--budgets", str(budgets)]) == 0


def test_over_budget_fails(budgets: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = cis.main(["--name", "search", "--size-bytes", str(100 * cis.MIB + 1), "--budgets", str(budgets)])
    assert code == 1
    assert "image size budget exceeded" in capsys.readouterr().err


def test_unbudgeted_image_is_a_notice_unless_required(budgets: Path) -> None:
    argv = ["--name", "newsvc", "--size-bytes", "1", "--budgets", str(budgets)]
    assert cis.main(argv) == 0
    assert cis.main([*argv, "--require-budget"]) == 1


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"images": []},
        {"images": {"x": {}}},
        {"images": {"x": {"max_mib": -1}}},
        {"images": {"x": {"max_mib": True}}},
    ],
)
def test_invalid_budget_files(tmp_path: Path, payload: Any) -> None:
    path = tmp_path / "b.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(cis.BudgetError):
        cis.load_budgets(path)


def test_docker_image_size_sums_uncompressed_layers() -> None:
    def fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        assert cmd[:2] == ["docker", "history"]
        assert "--human=false" in cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="100\n0\n23456\n", stderr="")

    assert cis.docker_image_size("img:tag", run=fake_run) == 23556


def test_docker_image_size_rejects_garbage() -> None:
    def fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout="12.3MB\n", stderr="")

    with pytest.raises(cis.BudgetError, match="unexpected layer size"):
        cis.docker_image_size("img:tag", run=fake_run)


def test_docker_image_size_surfaces_errors() -> None:
    def fake_run(cmd: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="No such image")

    with pytest.raises(cis.BudgetError, match="No such image"):
        cis.docker_image_size("missing:tag", run=fake_run)


def test_verdict_message() -> None:
    v = cis.check("gateway", 25 * cis.MIB, {"gateway": 50.0})
    assert v.ok
    assert v.message() == "gateway: 25.0 MiB / 50 MiB budget (50%, within)"

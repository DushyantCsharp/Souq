from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ci import verify_checksums as vc


def _write(root: Path, rel: str, data: bytes) -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "models"
    a = _write(root, "reranker-int8/model.onnx", b"reranker-bytes")
    b = _write(root, "bge-m3-int8/model.onnx", b"embedder-bytes")
    manifest = tmp_path / "checksums.txt"
    manifest.write_text(
        f"# sha256sum manifest\n{a}  reranker-int8/model.onnx\n{b} *./bge-m3-int8/model.onnx\n\n",
        encoding="utf-8",
    )
    return root, manifest


def test_parse_manifest_accepts_text_and_binary_mode(tree: tuple[Path, Path]) -> None:
    _, manifest = tree
    entries = vc.parse_manifest(manifest.read_text(encoding="utf-8"))
    assert set(entries) == {"reranker-int8/model.onnx", "bge-m3-int8/model.onnx"}


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("", "no entries"),
        ("deadbeef  file\n", "expected"),
        (f"{'a' * 64}  ../escape\n", "relative inside the root"),
        (f"{'a' * 64}  /etc/passwd\n", "relative inside the root"),
        (f"{'a' * 64}  x\n{'b' * 64}  ./x\n", "duplicate"),
    ],
)
def test_parse_manifest_rejects_bad_input(text: str, match: str) -> None:
    with pytest.raises(vc.ManifestError, match=match):
        vc.parse_manifest(text)


def test_all_present_files_match(tree: tuple[Path, Path]) -> None:
    root, manifest = tree
    assert vc.main(["--manifest", str(manifest), "--root", str(root), "--require-all"]) == 0


def test_tampered_file_fails(tree: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    root, manifest = tree
    (root / "reranker-int8/model.onnx").write_bytes(b"evil")
    assert vc.main(["--manifest", str(manifest), "--root", str(root)]) == 1
    assert "sha256 mismatch" in capsys.readouterr().err


def test_require_all_flags_absent_entries(tree: tuple[Path, Path]) -> None:
    root, manifest = tree
    (root / "bge-m3-int8/model.onnx").unlink()
    assert vc.main(["--manifest", str(manifest), "--root", str(root)]) == 0
    assert vc.main(["--manifest", str(manifest), "--root", str(root), "--require-all"]) == 1


def test_explicit_files_must_be_listed(tree: tuple[Path, Path]) -> None:
    root, manifest = tree
    _write(root, "reranker-int8/extra.bin", b"unlisted")
    entries = vc.parse_manifest(manifest.read_text(encoding="utf-8"))
    problems = vc.verify(entries, root, ["reranker-int8/model.onnx", "reranker-int8/extra.bin"])
    assert problems == [vc.Problem("reranker-int8/extra.bin", "not listed in manifest")]


def test_explicit_absolute_path(tree: tuple[Path, Path]) -> None:
    root, manifest = tree
    target = str(root / "reranker-int8/model.onnx")
    assert vc.main(["--manifest", str(manifest), "--root", str(root), target]) == 0


def test_empty_root_fails(tmp_path: Path, tree: tuple[Path, Path]) -> None:
    _, manifest = tree
    empty = tmp_path / "empty"
    empty.mkdir()
    assert vc.main(["--manifest", str(manifest), "--root", str(empty)]) == 1


def test_unreadable_manifest_exits_2(tmp_path: Path) -> None:
    assert vc.main(["--manifest", str(tmp_path / "nope.txt"), "--root", str(tmp_path)]) == 2


def test_no_extra_rejects_unlisted_files(tree: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    root, manifest = tree
    argv = ["--manifest", str(manifest), "--root", str(root), "--require-all", "--no-extra"]
    assert vc.main(argv) == 0
    _write(root, "reranker-int8/smuggled.so", b"x")
    assert vc.main(argv) == 1
    assert "reranker-int8/smuggled.so: present under root but not listed" in capsys.readouterr().err


def test_only_restricts_to_listed_dirs(tree: tuple[Path, Path]) -> None:
    root, manifest = tree
    (root / "reranker-int8/model.onnx").write_bytes(b"tampered")
    argv = ["--manifest", str(manifest), "--root", str(root), "--require-all"]
    assert vc.main(argv) == 1
    assert vc.main([*argv, "--only", "bge-m3-int8"]) == 0


def test_only_with_unknown_dir_is_a_manifest_error(tree: tuple[Path, Path]) -> None:
    root, manifest = tree
    assert vc.main(["--manifest", str(manifest), "--root", str(root), "--only", "nope"]) == 2

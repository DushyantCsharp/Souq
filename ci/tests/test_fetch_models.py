from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from ci import fetch_models as fm

FILES = {
    "reranker-int8/model.onnx": b"reranker",
    "reranker-int8/tokenizer.json": b"{}",
    "bge-m3-int8/model.onnx": b"embedder",
}


def _tar(path: Path, members: dict[str, bytes], *, symlink: tuple[str, str] | None = None) -> Path:
    with tarfile.open(path, "w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        if symlink is not None:
            info = tarfile.TarInfo(symlink[0])
            info.type = tarfile.SYMTYPE
            info.linkname = symlink[1]
            tf.addfile(info)
    return path


@pytest.fixture
def manifest(tmp_path: Path) -> Path:
    path = tmp_path / "checksums.txt"
    lines = [f"{hashlib.sha256(data).hexdigest()}  {name}" for name, data in FILES.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def archives(tmp_path: Path) -> Path:
    d = tmp_path / "assets"
    d.mkdir()
    _tar(d / "reranker-int8.tar.gz", {k: v for k, v in FILES.items() if k.startswith("reranker")})
    _tar(d / "bge-m3-int8.tar.gz", {k: v for k, v in FILES.items() if k.startswith("bge")})
    return d


def _run(archives: Path, dest: Path, manifest: Path) -> int:
    return fm.main(["--archives-dir", str(archives), "--dest", str(dest), "--manifest", str(manifest)])


def test_verified_archives_are_extracted(tmp_path: Path, archives: Path, manifest: Path) -> None:
    dest = tmp_path / "out"
    assert _run(archives, dest, manifest) == 0
    assert (dest / "reranker-int8/model.onnx").read_bytes() == b"reranker"


def test_tampered_archive_fails_and_cleans_up(tmp_path: Path, archives: Path, manifest: Path) -> None:
    _tar(archives / "bge-m3-int8.tar.gz", {"bge-m3-int8/model.onnx": b"tampered"})
    dest = tmp_path / "out"
    assert _run(archives, dest, manifest) == 1
    assert not dest.exists()


def test_unlisted_extra_file_fails(tmp_path: Path, archives: Path, manifest: Path) -> None:
    _tar(archives / "extra.tar.gz", {"reranker-int8/payload.so": b"x"})
    assert _run(archives, tmp_path / "out", manifest) == 1


def test_missing_asset_fails(tmp_path: Path, archives: Path, manifest: Path) -> None:
    (archives / "bge-m3-int8.tar.gz").unlink()
    assert _run(archives, tmp_path / "out", manifest) == 1


@pytest.mark.parametrize("name", ["../escape.onnx", "/abs/model.onnx"])
def test_path_traversal_is_rejected(tmp_path: Path, archives: Path, manifest: Path, name: str) -> None:
    _tar(archives / "evil.tar.gz", {name: b"x"})
    assert _run(archives, tmp_path / "out", manifest) == 1
    assert not (tmp_path / "escape.onnx").exists()


def test_symlink_escaping_dest_is_rejected(tmp_path: Path, archives: Path, manifest: Path) -> None:
    _tar(archives / "evil.tar.gz", {}, symlink=("reranker-int8/link", "../../../etc/passwd"))
    assert _run(archives, tmp_path / "out", manifest) == 1


def test_no_archives(tmp_path: Path, manifest: Path) -> None:
    empty = tmp_path / "none"
    empty.mkdir()
    assert _run(empty, tmp_path / "out", manifest) == 1


def test_dest_must_be_empty(tmp_path: Path, archives: Path, manifest: Path) -> None:
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "stale").write_text("x")
    assert _run(archives, dest, manifest) == 2


def test_tag_requires_repo(tmp_path: Path, manifest: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    code = fm.main(["--tag", "models-v1", "--dest", str(tmp_path / "o"), "--manifest", str(manifest)])
    assert code == 2


def test_only_subset_ignores_other_models(tmp_path: Path, archives: Path, manifest: Path) -> None:
    # A broken reranker archive must not matter for an image that only needs the embedder.
    _tar(archives / "reranker-int8.tar.gz", {"reranker-int8/model.onnx": b"tampered"})
    dest = tmp_path / "out"
    argv = ["--archives-dir", str(archives), "--dest", str(dest), "--manifest", str(manifest)]
    assert fm.main([*argv, "--only", "bge-m3-int8"]) == 0
    assert sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()) == [
        "bge-m3-int8/model.onnx"
    ]

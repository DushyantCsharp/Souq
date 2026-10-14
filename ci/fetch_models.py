#!/usr/bin/env python3
"""Fetch the search image's ONNX artefacts from a GitHub release and verify them before baking.

    fetch_models.py --tag models-v1 --dest "$RUNNER_TEMP/models"            # downloads with `gh`
    fetch_models.py --archives-dir ./downloaded --dest ./models-verified    # already-downloaded assets

Every ``*.tar.gz`` asset is extracted into ``--dest`` with tarfile's ``data`` filter (rejects absolute
paths, ``..`` traversal, links escaping the destination, device files), then the whole tree is checked
with ``verify_checksums.py --require-all --no-extra`` against ``models/checksums.txt``: every listed file
must be present with the committed digest and nothing unlisted may ride along into the image.
``--only DIR`` (repeatable) fetches just ``DIR.tar.gz`` and verifies just the manifest entries under
``DIR/`` — the indexer image needs the embedder but not the reranker.
Exit codes: 0 verified, 1 verification/extraction failure, 2 usage or download error.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Sequence
from pathlib import Path

VERIFY = Path(__file__).with_name("verify_checksums.py")


def download(tag: str, repo: str, into: Path, only: Sequence[str]) -> None:
    patterns = [f"{d}.tar.gz" for d in only] or ["*.tar.gz"]
    cmd = ["gh", "release", "download", tag, "--repo", repo, "--dir", str(into)]
    for pattern in patterns:
        cmd += ["--pattern", pattern]
    subprocess.run(cmd, check=True)


def extract_all(archives: Sequence[Path], dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for archive in archives:
        with tarfile.open(archive, mode="r:gz") as tf:
            tf.extractall(dest, filter="data")


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Download, extract and verify ONNX model release assets.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--tag", help="GitHub release tag holding the model tarballs")
    src.add_argument("--archives-dir", type=Path, help="use already-downloaded *.tar.gz files")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"), help="owner/name (default: env)")
    ap.add_argument("--dest", type=Path, required=True, help="empty directory to extract into")
    ap.add_argument("--manifest", type=Path, default=Path("models/checksums.txt"))
    ap.add_argument("--only", action="append", default=[], metavar="DIR", help="only this model dir")
    args = ap.parse_args(argv)
    only = [d.strip("/") for d in args.only]

    if args.dest.exists() and any(args.dest.iterdir()):
        print(f"fetch_models: {args.dest} must be empty", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="souq-models-") as tmp:
        archives_dir = args.archives_dir
        if archives_dir is None:
            if not args.repo:
                print("fetch_models: --repo or GITHUB_REPOSITORY is required with --tag", file=sys.stderr)
                return 2
            archives_dir = Path(tmp)
            try:
                download(args.tag, args.repo, archives_dir, only)
            except (OSError, subprocess.CalledProcessError) as exc:
                print(f"fetch_models: download of release {args.tag} failed: {exc}", file=sys.stderr)
                return 2
        archives = sorted(archives_dir.glob("*.tar.gz"))
        if only:
            archives = [a for a in archives if a.name.removesuffix(".tar.gz") in only]
        if not archives:
            print(f"fetch_models: no *.tar.gz assets in {archives_dir}", file=sys.stderr)
            return 1
        try:
            extract_all(archives, args.dest)
        except (tarfile.TarError, OSError) as exc:
            print(f"::error title=model archive rejected::{exc}", file=sys.stderr)
            shutil.rmtree(args.dest, ignore_errors=True)
            return 1

    verify = subprocess.run(
        [
            sys.executable,
            str(VERIFY),
            "--manifest",
            str(args.manifest),
            "--root",
            str(args.dest),
            "--require-all",
            "--no-extra",
            *(arg for d in only for arg in ("--only", d)),
        ],
        check=False,
    )
    if verify.returncode != 0:
        # Never leave unverified bytes where a later step could pick them up.
        shutil.rmtree(args.dest, ignore_errors=True)
        return 1
    print(f"fetch_models: {len(archives)} archive(s) extracted and verified into {args.dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

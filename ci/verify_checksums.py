#!/usr/bin/env python3
"""Verify files against a sha256sum-format manifest (``models/checksums.txt``).

Used by the release workflow before model artefacts are baked into the search image: a release asset
whose digest does not match the committed manifest must never ship, so this fails closed on every
ambiguity (unlisted file, missing file, malformed line, duplicate entry).

Manifest lines: ``<64 hex>  <relative path>`` (``sha256sum`` text mode) or ``<hex> *<path>`` (binary
mode). Blank lines and ``#`` comments are ignored. Paths are relative to ``--root``.

    verify_checksums.py --manifest models/checksums.txt --root DIR [--require-all] [FILE ...]

With FILE arguments only those files are checked (each must be listed). Without, every manifest entry
present under ``--root`` is checked; ``--require-all`` additionally fails on entries that are absent and
``--no-extra`` on files under ``--root`` that the manifest does not list (so nothing unverified is baked).
``--only DIR`` (repeatable) restricts the manifest to entries under those top-level directories, for an
image that needs a subset of the models (the indexer only embeds; it never reranks).
Exit codes: 0 ok, 1 mismatch/missing, 2 unusable manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_LINE = re.compile(r"^(?P<digest>[0-9a-fA-F]{64}) (?P<mode>[ *])(?P<path>.+)$")
_CHUNK = 1 << 20


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class Problem:
    path: str
    reason: str


def parse_manifest(text: str) -> dict[str, str]:
    entries: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE.match(line)
        if m is None:
            raise ManifestError(f"line {lineno}: expected '<sha256>  <path>', got {raw!r}")
        rel = _normalise(m.group("path"), lineno)
        if rel in entries:
            raise ManifestError(f"line {lineno}: duplicate entry for {rel}")
        entries[rel] = m.group("digest").lower()
    if not entries:
        raise ManifestError("manifest has no entries")
    return entries


def _normalise(path: str, lineno: int) -> str:
    p = PurePosixPath(path.strip().removeprefix("./"))
    # A manifest entry escaping --root would let an asset overwrite arbitrary files when extracted.
    if p.is_absolute() or ".." in p.parts:
        raise ManifestError(f"line {lineno}: path must be relative inside the root: {path!r}")
    return p.as_posix()


def restrict(entries: dict[str, str], only: Sequence[str]) -> dict[str, str]:
    if not only:
        return entries
    wanted = {d.strip("/") for d in only}
    return {rel: digest for rel, digest in entries.items() if rel.split("/", 1)[0] in wanted}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def verify(
    entries: dict[str, str],
    root: Path,
    files: Iterable[str] = (),
    *,
    require_all: bool = False,
    no_extra: bool = False,
) -> list[Problem]:
    problems: list[Problem] = []
    selected = [_relative_to_root(f, root) for f in files]
    if selected:
        targets = []
        for rel in selected:
            if rel not in entries:
                problems.append(Problem(rel, "not listed in manifest"))
            else:
                targets.append(rel)
    else:
        targets = sorted(entries)

    for rel in targets:
        path = root / rel
        if not path.is_file():
            if selected or require_all:
                problems.append(Problem(rel, "missing"))
            continue
        actual = sha256_file(path)
        if actual != entries[rel]:
            problems.append(Problem(rel, f"sha256 mismatch: expected {entries[rel]}, got {actual}"))
    if not selected and not require_all and not any((root / rel).is_file() for rel in entries):
        problems.append(Problem(str(root), "none of the manifest entries exist under root"))
    if no_extra:
        for path in sorted(p for p in root.rglob("*") if p.is_file() or p.is_symlink()):
            rel = path.relative_to(root).as_posix()
            if rel not in entries:
                problems.append(Problem(rel, "present under root but not listed in manifest"))
    return problems


def _relative_to_root(file: str, root: Path) -> str:
    p = Path(file)
    if p.is_absolute():
        try:
            p = p.resolve().relative_to(root.resolve())
        except ValueError:
            return p.as_posix()
    return PurePosixPath(p.as_posix().removeprefix("./")).as_posix()


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Verify files against a sha256sum manifest.")
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--root", type=Path, required=True, help="directory manifest paths are relative to")
    ap.add_argument("--require-all", action="store_true", help="fail if any manifest entry is absent")
    ap.add_argument("--no-extra", action="store_true", help="fail on files under --root not in manifest")
    ap.add_argument(
        "--only", action="append", default=[], metavar="DIR", help="restrict to entries under DIR/"
    )
    ap.add_argument("files", nargs="*", help="check only these files (relative to --root or absolute)")
    args = ap.parse_args(argv)

    try:
        entries = restrict(parse_manifest(args.manifest.read_text(encoding="utf-8")), args.only)
        if not entries:
            raise ManifestError(f"no entries under {', '.join(args.only)}")
    except (OSError, ManifestError) as exc:
        print(f"verify_checksums: {args.manifest}: {exc}", file=sys.stderr)
        return 2

    problems = verify(entries, args.root, args.files, require_all=args.require_all, no_extra=args.no_extra)
    for p in problems:
        print(f"::error title=checksum::{p.path}: {p.reason}", file=sys.stderr)
    if problems:
        return 1
    checked = len(args.files) if args.files else sum((args.root / r).is_file() for r in entries)
    print(f"verify_checksums: {checked} file(s) match {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

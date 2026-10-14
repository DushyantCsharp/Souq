"""Write (or verify) models/checksums.txt for the runtime artefacts the search image ships.

    uv run --package souq-models python models/scripts/write_checksums.py           # write
    uv run --package souq-models python models/scripts/write_checksums.py --check   # verify local files

Format is ``sha256sum``: ``<sha256>  <path>`` with paths relative to models/artifacts/, one line per file of
each runtime dir (reranker-int8/, bge-m3-int8/). CI verifies release tarballs against it
(ci/verify_checksums.py --manifest models/checksums.txt --root <extracted dir>).
"""

from __future__ import annotations

import argparse
import sys

from souq_common.embeddings import repo_root
from souq_models.hub import sha256_file
from souq_models.registry import ARTIFACTS, RUNTIME_DIRS

MANIFEST = repo_root() / "models" / "checksums.txt"
RUNTIME_FILES = ("model.onnx", "tokenizer.json", "souq_model.json")


def entries() -> list[tuple[str, str]]:
    out = []
    for name in sorted(RUNTIME_DIRS.values()):
        for fname in RUNTIME_FILES:
            path = ARTIFACTS / name / fname
            if not path.exists():
                raise FileNotFoundError(f"{path} missing; run models/scripts/export.py --all --install")
            out.append((sha256_file(path), f"{name}/{fname}"))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        bad = 0
        for line in MANIFEST.read_text().splitlines():
            digest, rel = line.split("  ", 1)
            path = ARTIFACTS / rel
            ok = path.exists() and sha256_file(path) == digest
            bad += not ok
            print(f"{'OK ' if ok else 'BAD'} {rel}")
        return 1 if bad else 0
    lines = [f"{digest}  {rel}" for digest, rel in entries()]
    MANIFEST.write_text("\n".join(lines) + "\n")
    print(MANIFEST.read_text(), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())

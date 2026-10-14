"""Pack each runtime model dir as models/artifacts/release/<dir>.tar.gz for the GitHub release that CI
downloads (tag ``models-v1`` by default; see models/README.md).

    uv run --package souq-models python models/scripts/package_release.py
    gh release create models-v1 models/artifacts/release/*.tar.gz --title "Search models v1" \\
        --notes-file models/README.md        # or: gh release upload models-v1 ... --clobber
"""

from __future__ import annotations

import tarfile

from write_checksums import RUNTIME_FILES

from souq_models.registry import ARTIFACTS, RUNTIME_DIRS


def main() -> int:
    out_dir = ARTIFACTS / "release"
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in sorted(RUNTIME_DIRS.values()):
        target = out_dir / f"{name}.tar.gz"
        with tarfile.open(target, "w:gz") as tar:
            for fname in RUNTIME_FILES:
                tar.add(ARTIFACTS / name / fname, arcname=f"{name}/{fname}")
        print(f"{target} ({target.stat().st_size / 1e6:.0f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

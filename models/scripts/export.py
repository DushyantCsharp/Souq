"""Download (pinned, sha256-verified) and export models to ONNX int8 under models/artifacts/.

    uv run --package souq-models python models/scripts/export.py mmarco-mMiniLMv2-L12-H384-v1 bge-m3
    uv run --package souq-models python models/scripts/export.py --all --install

``--install`` copies the default reranker to ``artifacts/reranker-int8`` and bge-m3 to
``artifacts/bge-m3-int8`` (the directories SOUQ_RERANKER_MODEL / SOUQ_EMBEDDER_MODEL point at).
"""

from __future__ import annotations

import argparse
import logging
import time

from souq_models.hub import fetch
from souq_models.onnx_tools import export, install
from souq_models.registry import DEFAULT_RERANKER, MODELS, RUNTIME_DIRS


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("models", nargs="*", choices=[[], *MODELS], help="model keys (default: all)")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--install", action="store_true", help="copy defaults to the runtime dirs")
    parser.add_argument("--no-fuse", action="store_true", help="skip transformer graph fusion")
    parser.add_argument("--skip-verify", action="store_true", help="skip sha256 of already-downloaded files")
    args = parser.parse_args()
    keys = list(MODELS) if args.all or not args.models else args.models
    for key in keys:
        spec = MODELS[key]
        t = time.perf_counter()
        fetch(spec, verify=not args.skip_verify)
        out = export(spec, fuse=not args.no_fuse)
        print(f"{key}: {out} ({time.perf_counter() - t:.0f}s)")
        if args.install:
            if spec.kind == "embedder":
                print("  installed", install(out, RUNTIME_DIRS["embedder"]))
            elif key == DEFAULT_RERANKER:
                print("  installed", install(out, RUNTIME_DIRS["reranker"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

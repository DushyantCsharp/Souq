"""Write the internal search OpenAPI spec to packages/contracts/search-internal/openapi.json.

The contracts agent generates the gateway's search client from that file, so run this after any change
to souq_search.models or the routes (``pnpm --filter @souq/search openapi``), then
``pnpm --filter @souq/contracts codegen``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("OTEL_SDK_DISABLED", "true")

from souq_search.app import create_app

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUT = ROOT / "packages" / "contracts" / "search-internal" / "openapi.json"


def render() -> str:
    spec = create_app(components=None).openapi()
    return json.dumps(spec, indent=2, sort_keys=False, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--check", action="store_true", help="exit 1 if the file is out of date")
    args = parser.parse_args()
    text = render()
    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.exists() else ""
        if current != text:
            print(f"{args.out} is out of date; run scripts/export_openapi.py", file=sys.stderr)
            return 1
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

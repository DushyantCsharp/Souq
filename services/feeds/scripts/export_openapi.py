"""Write the feeds FastAPI OpenAPI document to packages/contracts/feeds-internal/openapi.json.

The contracts package generates the gateway's feeds client from it and checks it for drift. Format as
agreed with the contracts agent: ``json.dumps(spec, indent=2)`` + newline. Needs no database.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from souq_feeds.api import create_app

REPO = Path(__file__).resolve().parents[3]
TARGET = REPO / "packages" / "contracts" / "feeds-internal" / "openapi.json"


def render() -> str:
    spec = create_app().openapi()
    return json.dumps(spec, indent=2) + "\n"


def main() -> int:
    text = render()
    if "--check" in sys.argv:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != text:
            print(f"{TARGET} is stale; run: pnpm --filter @souq/feeds codegen", file=sys.stderr)
            return 1
        return 0
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(text, encoding="utf-8")
    print(f"wrote {TARGET.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""``souq-search`` entrypoint: uvicorn on HOST/PORT (default 0.0.0.0:8001)."""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "souq_search.app:create_app",
        factory=True,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8001")),
        workers=int(os.environ.get("WEB_CONCURRENCY", "1")),
        log_level=os.environ.get("LOG_LEVEL", "info"),
        access_log=False,
    )


if __name__ == "__main__":
    main()

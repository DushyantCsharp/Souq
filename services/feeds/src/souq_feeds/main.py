"""``souq-feeds`` entry point (uvicorn on HOST:PORT, default 0.0.0.0:8002)."""

from __future__ import annotations

import uvicorn

from souq_common.telemetry import init_telemetry
from souq_feeds.api import create_app
from souq_feeds.config import bind_host, bind_port
from souq_feeds.logs import configure_logging


def main() -> None:
    configure_logging("souq-feeds")
    init_telemetry("feeds")
    # Graceful shutdown: in-flight imports stop at a batch boundary and are re-queued (see ImportRunner).
    uvicorn.run(
        create_app(), host=bind_host(), port=bind_port(), log_config=None, timeout_graceful_shutdown=30
    )


if __name__ == "__main__":
    main()

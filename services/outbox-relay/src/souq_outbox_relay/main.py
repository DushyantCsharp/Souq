"""``souq-outbox-relay`` entry point."""

from __future__ import annotations

import dataclasses
import logging
import os
import signal
import time
from types import FrameType
from typing import Any

import psycopg

from souq_common.telemetry import init_telemetry
from souq_outbox_relay.config import RelaySettings, load_settings
from souq_outbox_relay.health import HeartbeatFile, serve_health
from souq_outbox_relay.kafka import make_producer
from souq_outbox_relay.logs import configure_logging
from souq_outbox_relay.relay import OutboxRelay

log = logging.getLogger("souq_outbox_relay")


def build_relay(settings: RelaySettings) -> OutboxRelay:
    def connect() -> psycopg.Connection[Any]:
        return psycopg.connect(
            settings.database_url, autocommit=True, application_name=settings.client_id, connect_timeout=5
        )

    return OutboxRelay(
        connect,
        make_producer(settings.kafka_bootstrap, settings.client_id),
        topic=settings.topic,
        batch_size=settings.batch_size,
        poll_interval_s=settings.poll_interval_ms / 1000,
        delivery_timeout_s=settings.delivery_timeout_s,
        retention_s=settings.retention_hours * 3600,
        prune_interval_s=settings.prune_interval_s,
        backlog_interval_s=settings.backlog_interval_s,
        heartbeat=HeartbeatFile(os.environ.get("SOUQ_HEALTH_FILE", "/tmp/souq-outbox-relay.alive")).beat,
    )


def main() -> None:
    configure_logging("souq-outbox-relay")
    init_telemetry("outbox-relay")
    settings = load_settings()
    relay = build_relay(settings)

    def on_signal(signum: int, _frame: FrameType | None) -> None:
        log.info("signal %d: finishing the in-flight batch, then stopping", signum)
        relay.request_stop()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    def live() -> tuple[bool, dict[str, Any]]:
        age = time.monotonic() - relay.stats.heartbeat
        return age < 2 * settings.delivery_timeout_s + 30, {"loop_age_s": round(age, 1)}

    def ready() -> tuple[bool, dict[str, Any]]:
        ok = not relay.stopping and relay.stats.last_error is None
        return ok, {"last_error": relay.stats.last_error}

    serve_health(settings.health_port, live, ready, lambda: dataclasses.asdict(relay.stats))
    relay.run()


if __name__ == "__main__":
    main()

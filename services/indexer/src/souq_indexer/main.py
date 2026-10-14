"""``souq-indexer`` entry point."""

from __future__ import annotations

import dataclasses
import logging
import os
import signal
import time
from types import FrameType
from typing import Any

from opensearchpy import OpenSearch

from souq_common.embeddings import get_embedder
from souq_common.telemetry import init_telemetry
from souq_indexer.aimd import AimdController
from souq_indexer.aliases import WriteAliasResolver
from souq_indexer.config import IndexerSettings, load_settings
from souq_indexer.health import HeartbeatFile, serve_health
from souq_indexer.kafka_io import make_consumer, make_producer
from souq_indexer.logs import configure_logging
from souq_indexer.pipeline import BatchProcessor
from souq_indexer.service import IndexerService

log = logging.getLogger("souq_indexer")


def make_opensearch(url: str, timeout_s: float) -> OpenSearch:
    # Retries are ours (per item, with backpressure); the client must not silently re-send a bulk.
    return OpenSearch(hosts=[url], timeout=timeout_s, max_retries=0, retry_on_timeout=False, pool_maxsize=4)


def build_service(settings: IndexerSettings) -> IndexerService:
    client = make_opensearch(settings.opensearch_url, settings.bulk_timeout_ms / 1000)
    resolver = WriteAliasResolver(client, settings.write_alias, settings.alias_refresh_s)
    aimd = AimdController(
        initial=settings.batch_initial,
        min_size=settings.batch_min,
        max_size=settings.batch_max,
        additive_step=settings.batch_step,
    )
    consumer = make_consumer(settings.kafka_bootstrap, settings.group_id, settings.client_id)
    producer = make_producer(settings.kafka_bootstrap, settings.client_id)
    holder: dict[str, IndexerService] = {}

    def idle(seconds: float) -> None:
        holder["svc"].idle(seconds)

    def should_stop() -> bool:
        return holder["svc"].stopping

    processor = BatchProcessor(
        client,
        resolver,
        get_embedder(),
        aimd,
        in_process_attempts=settings.in_process_attempts,
        bulk_timeout_s=settings.bulk_timeout_ms / 1000,
        max_bulk_bytes=settings.max_bulk_bytes,
        embed_batch=settings.embed_batch,
        idle=idle,
        should_stop=should_stop,
    )
    svc = IndexerService(
        consumer,
        producer,
        processor,
        aimd,
        batch_linger_ms=settings.batch_linger_ms,
        lag_report_interval_s=settings.lag_report_interval_s,
        dlq_depth_interval_s=settings.dlq_depth_interval_s,
        resolver=resolver,
        heartbeat=HeartbeatFile(os.environ.get("SOUQ_HEALTH_FILE", "/tmp/souq-indexer.alive")).beat,
    )
    holder["svc"] = svc
    return svc


def main() -> None:
    configure_logging("souq-indexer")
    init_telemetry("indexer")
    settings = load_settings()
    svc = build_service(settings)

    def on_signal(signum: int, _frame: FrameType | None) -> None:
        log.info("signal %d: finishing the in-flight batch, then stopping", signum)
        svc.request_stop()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    def live() -> tuple[bool, dict[str, Any]]:
        age = time.monotonic() - svc.stats.heartbeat
        # A single bulk can legitimately take bulk_timeout; beyond 2x that the loop is wedged.
        return age < max(60.0, 2 * settings.bulk_timeout_ms / 1000), {"loop_age_s": round(age, 1)}

    def ready() -> tuple[bool, dict[str, Any]]:
        ok = not svc.stopping and bool(svc.stats.write_indices) and not svc.processor.backpressure_active
        return ok, {
            "assigned_partitions": svc.stats.assigned,
            "write_indices": svc.stats.write_indices,
            "backpressure": svc.processor.backpressure_active,
        }

    def stats() -> dict[str, Any]:
        out = dataclasses.asdict(svc.stats)
        out["batch_size"] = svc.aimd.batch_size
        out["backpressure"] = svc.processor.backpressure_active
        out["freshness"] = svc.freshness_summary()
        return out

    serve_health(settings.health_port, live, ready, stats)
    log.info(
        "indexer starting: kafka=%s opensearch=%s alias=%s group=%s",
        settings.kafka_bootstrap,
        settings.opensearch_url,
        settings.write_alias,
        settings.group_id,
    )
    svc.run()


if __name__ == "__main__":
    main()

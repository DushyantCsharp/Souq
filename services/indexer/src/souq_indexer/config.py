"""Indexer settings. Shared endpoints come from ``souq_common.settings``; timing budgets from budgets.yaml."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from souq_common.budgets import load_budgets
from souq_common.settings import get_settings


class IndexerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="INDEXER_", env_file=".env", extra="ignore")

    group_id: str = "souq-indexer"
    client_id: str = "souq-indexer"
    health_port: int = 8003
    # AIMD bounds (CLAUDE.md contract defaults).
    batch_initial: int = 1000
    batch_min: int = 50
    batch_max: int = 5000
    batch_step: int = 100
    # A dual-written 1024-d embedding is ~20 KB per action pair; cap request size well below
    # OpenSearch's 100 MB http.max_content_length so a large AIMD batch is split, not refused.
    max_bulk_bytes: int = 10 * 1024 * 1024
    alias_refresh_s: float = 10.0
    lag_report_interval_s: float = 5.0
    dlq_depth_interval_s: float = 30.0
    embed_batch: int = 256

    # Resolved from shared settings / budgets.yaml at construction (see load()).
    kafka_bootstrap: str = ""
    opensearch_url: str = ""
    write_alias: str = ""
    batch_linger_ms: float = 0.0
    bulk_timeout_ms: float = 0.0
    in_process_attempts: int = 0


@lru_cache(maxsize=1)
def load_settings() -> IndexerSettings:
    shared = get_settings()
    budgets = load_budgets().indexer
    base = IndexerSettings()
    return base.model_copy(
        update={
            "kafka_bootstrap": base.kafka_bootstrap or shared.kafka_bootstrap,
            "opensearch_url": base.opensearch_url or shared.opensearch_url,
            "write_alias": base.write_alias or shared.write_alias,
            "batch_linger_ms": base.batch_linger_ms or float(budgets.batch_linger_ms),
            "bulk_timeout_ms": base.bulk_timeout_ms or float(budgets.bulk_timeout_ms),
            "in_process_attempts": base.in_process_attempts or int(budgets.in_process_attempts),
        }
    )

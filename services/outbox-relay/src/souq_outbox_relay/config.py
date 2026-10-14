"""Relay settings: endpoints from ``souq_common.settings``, batch/poll cadence from budgets.yaml."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from souq_common.budgets import load_budgets
from souq_common.settings import get_settings


class RelaySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RELAY_", env_file=".env", extra="ignore")

    client_id: str = "souq-outbox-relay"
    topic: str = "catalog.changes"
    health_port: int = 8004
    delivery_timeout_s: float = 30.0
    # Published rows are kept this long for debugging/replay, then pruned in small chunks. 0 disables.
    retention_hours: float = 72.0
    prune_interval_s: float = 600.0
    backlog_interval_s: float = 10.0

    database_url: str = ""
    kafka_bootstrap: str = ""
    batch_size: int = 0
    poll_interval_ms: float = 0.0


@lru_cache(maxsize=1)
def load_settings() -> RelaySettings:
    shared = get_settings()
    budgets = load_budgets().relay
    base = RelaySettings()
    return base.model_copy(
        update={
            "database_url": base.database_url or shared.database_url,
            "kafka_bootstrap": base.kafka_bootstrap or shared.kafka_bootstrap,
            "batch_size": base.batch_size or int(budgets.batch_size),
            "poll_interval_ms": base.poll_interval_ms or float(budgets.poll_interval_ms),
        }
    )

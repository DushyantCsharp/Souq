"""Process settings from environment variables (see .env.example)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgres://souq:souq@localhost:55432/souq"
    redis_url: str = "redis://localhost:6379/0"
    kafka_bootstrap: str = "localhost:19092"
    opensearch_url: str = "http://localhost:9200"
    search_url: str = "http://localhost:8001"
    feeds_url: str = "http://localhost:8002"

    anthropic_api_key: str | None = None
    souq_llm_mode: Literal["replay", "record", "live"] = "replay"
    souq_llm_model: str = "claude-haiku-5-5"
    souq_embedder: Literal["bge-m3", "fake"] = "fake"
    souq_reranker_model: str = "models/artifacts/reranker-int8"
    souq_config_dir: str | None = None

    read_alias: str = "products_read"
    write_alias: str = "products_write"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

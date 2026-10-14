"""Feeds service settings."""

from __future__ import annotations

import os
import tempfile
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from souq_common.settings import get_settings


class FeedsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FEEDS_", env_file=".env", extra="ignore")

    upload_dir: Path = Path(tempfile.gettempdir()) / "souq-feeds"
    max_upload_bytes: int = 2 * 1024**3
    # Rows per transaction (products + product_texts + outbox). The brief fixes 1k.
    batch_size: int = 1000
    import_workers: int = 2
    pool_max: int = 8
    database_url: str = ""


@lru_cache(maxsize=1)
def load_settings() -> FeedsSettings:
    base = FeedsSettings()
    return base.model_copy(update={"database_url": base.database_url or get_settings().database_url})


def bind_host() -> str:
    return os.environ.get("HOST", "0.0.0.0")


def bind_port() -> int:
    return int(os.environ.get("PORT", "8002"))

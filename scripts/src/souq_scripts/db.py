"""Postgres connection helper (psycopg 3)."""

from __future__ import annotations

from typing import Any

import psycopg

from souq_common.settings import get_settings


def database_url(override: str | None = None) -> str:
    return override or get_settings().database_url


def connect(override: str | None = None, *, autocommit: bool = False) -> psycopg.Connection[tuple[Any, ...]]:
    return psycopg.connect(database_url(override), autocommit=autocommit, application_name="souq-scripts")

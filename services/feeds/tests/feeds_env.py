"""Helpers for feeds tests: sample feed paths and a throwaway database with the real migrations."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

REPO = Path(__file__).resolve().parents[3]
MIGRATIONS = REPO / "packages" / "db" / "migrations"
SAMPLES = REPO / "scripts" / "seed" / "samples"


def up_sections() -> list[str]:
    out = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        out.append(re.split(r"^-- Down Migration", text, flags=re.M)[0].replace("-- Up Migration", ""))
    return out


@contextmanager
def scratch_database(base_dsn: str) -> Iterator[str]:
    """CREATE DATABASE + apply packages/db migrations; dropped afterwards (safe on a shared server)."""
    name = f"it_feeds_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(base_dsn, autocommit=True, connect_timeout=3) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    params = conninfo_to_dict(base_dsn)
    params["dbname"] = name
    dsn = make_conninfo(**params)  # type: ignore[arg-type]
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for sql in up_sections():
                conn.execute(sql)  # type: ignore[call-overload]
        yield dsn
    finally:
        with psycopg.connect(base_dsn, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')

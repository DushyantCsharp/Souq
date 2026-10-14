"""Isolated resources for integration tests on a shared stack: a throwaway database on the configured
Postgres server (with the real packages/db migrations applied) and a throwaway Kafka topic. Nothing the
other services read is touched, and everything is dropped afterwards."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
from confluent_kafka.admin import AdminClient, NewTopic
from psycopg.conninfo import conninfo_to_dict, make_conninfo

MIGRATIONS = Path(__file__).resolve().parents[3] / "packages" / "db" / "migrations"


def up_sections(only: str | None = None) -> list[str]:
    """The ``-- Up Migration`` part of each node-pg-migrate SQL file, in filename order."""
    out = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if only and only not in path.name:
            continue
        text = path.read_text(encoding="utf-8")
        up = re.split(r"^-- Down Migration", text, flags=re.M)[0]
        out.append(up.replace("-- Up Migration", ""))
    return out


@contextmanager
def scratch_database(base_dsn: str, only_migration: str | None = None) -> Iterator[str]:
    name = f"it_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(base_dsn, autocommit=True, connect_timeout=3) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    params = conninfo_to_dict(base_dsn)
    params["dbname"] = name
    dsn = make_conninfo(**params)  # type: ignore[arg-type]
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            for sql in up_sections(only_migration):
                conn.execute(sql)  # type: ignore[call-overload]
        yield dsn
    finally:
        with psycopg.connect(base_dsn, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@contextmanager
def scratch_topic(bootstrap: str, partitions: int = 3) -> Iterator[str]:
    admin = AdminClient({"bootstrap.servers": bootstrap})
    name = f"it.{uuid.uuid4().hex[:10]}"
    admin.create_topics([NewTopic(name, num_partitions=partitions, replication_factor=1)])[name].result(10)
    try:
        yield name
    finally:
        admin.delete_topics([name])[name].result(10)

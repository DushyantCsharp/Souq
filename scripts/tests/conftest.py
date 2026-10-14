"""Fixtures for souq-scripts tests. Integration fixtures use the compose stack and skip when it is down."""

from __future__ import annotations

import os
import secrets
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import pytest

from souq_scripts.paths import ROOT

ADMIN_URL = os.environ.get("DATABASE_URL", "postgres://souq:souq@localhost:55432/souq")
OPENSEARCH_URL = os.environ.get("OPENSEARCH_URL", "http://localhost:9200")
MIGRATE = ROOT / "packages" / "db" / "src" / "migrate.ts"


def _with_db(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))


def migrate(url: str, *args: str) -> None:
    """Run packages/db migrations (node-pg-migrate) against ``url``."""
    subprocess.run(
        ["node", str(MIGRATE), *args],
        check=True,
        cwd=MIGRATE.parent.parent,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
    )


@pytest.fixture
def scratch_db() -> Iterator[str]:
    """An empty throwaway database on the compose Postgres; dropped afterwards."""
    psycopg = pytest.importorskip("psycopg")
    try:
        admin = psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError:
        pytest.skip(f"Postgres not reachable at {ADMIN_URL}")
    name = f"souq_scripts_test_{secrets.token_hex(4)}"
    admin.execute(f"CREATE DATABASE {name}")
    try:
        yield _with_db(ADMIN_URL, name)
    finally:
        admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        admin.close()


@pytest.fixture
def migrated_db(scratch_db: str) -> str:
    migrate(scratch_db, "up")
    return scratch_db


@pytest.fixture
def run_migrations() -> Any:
    """migrate() as a fixture: under --import-mode=importlib tests cannot import conftest directly."""
    return migrate


@pytest.fixture
def pre_expand() -> str:
    """How many migrations precede the size expand migration (the schema old writers knew)."""
    files = sorted(p.name for p in (ROOT / "packages" / "db" / "migrations").glob("*.sql"))
    return str(next(i for i, f in enumerate(files) if f.endswith("_expand_product_size.sql")))


@pytest.fixture(scope="session")
def opensearch() -> Any:
    from opensearchpy import OpenSearch

    client = OpenSearch(OPENSEARCH_URL, timeout=60)
    try:
        if not client.ping():
            raise ConnectionError
    except Exception:
        pytest.skip(f"OpenSearch not reachable at {OPENSEARCH_URL}")
    return client


@pytest.fixture
def tmp_templates(tmp_path: Path) -> Path:
    import shutil

    from souq_scripts.paths import TEMPLATES_DIR

    dst = tmp_path / "templates"
    shutil.copytree(TEMPLATES_DIR, dst)
    return dst

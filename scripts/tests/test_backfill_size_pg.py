"""Expand/contract on a real Postgres: rows written before the expand migration get backfilled."""

from __future__ import annotations

import json
from typing import Any

import pytest

from souq_scripts.backfill_size import backfill, pending

pytestmark = pytest.mark.integration


def test_backfill_fills_size_without_touching_versions(
    scratch_db: str, run_migrations: Any, pre_expand: str
) -> None:
    import psycopg

    run_migrations(scratch_db, "up", pre_expand)  # schema as it was before the expand migration
    with psycopg.connect(scratch_db) as conn:
        for i in range(7):
            attrs = {"size": f"S{i}", "material": "cotton"} if i != 3 else {"material": "wool"}
            conn.execute(
                "INSERT INTO products (sku, brand, category_path, price, currency, attributes) "
                "VALUES (%s, 'Acme', 'fashion.shirts', 10, 'AED', %s)",
                (f"P{i}", json.dumps(attrs)),
            )
        conn.commit()
        before = conn.execute("SELECT sku, version, updated_at FROM products ORDER BY sku").fetchall()

    run_migrations(scratch_db, "up")  # expand: adds products.size + sync trigger
    with psycopg.connect(scratch_db, autocommit=True) as conn:
        assert pending(conn) == 6
        assert conn.execute("SELECT product_document('P2')->>'size'").fetchone() == ("S2",), "coalesce path"
        assert backfill(conn, batch_size=2, sleep_ms=0) == 6
        assert pending(conn) == 0
        assert backfill(conn, batch_size=2, sleep_ms=0) == 0, "re-running is a no-op"
        rows = conn.execute("SELECT sku, size, version, updated_at FROM products ORDER BY sku").fetchall()
        assert [(s, v, u) for s, _, v, u in rows] == before, "a storage-only backfill must not bump versions"
        assert [size for _, size, _, _ in rows] == ["S0", "S1", "S2", None, "S4", "S5", "S6"]

    run_migrations(scratch_db, "up", "--include-next")  # contract: drop trigger, strip attributes.size
    with psycopg.connect(scratch_db) as conn:
        doc = conn.execute("SELECT product_document('P2')").fetchone()[0]
        assert doc["size"] == "S2"
        assert doc["attributes"] == {"material": "cotton"}

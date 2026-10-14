"""Postgres helpers: run the packages/db migrations and write products the way the feeds importer does
(products + product_texts + outbox in one transaction, payload = ``product_document(sku)``).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterable
from typing import Any

from souq_testing.factories import Product
from souq_testing.paths import DB_PACKAGE_DIR


def migrate_up(database_url: str) -> str:
    """Apply every release migration with the real runner (node-pg-migrate) — the same command CI and
    the compose stack use, so the suite fails if a migration is broken for a bare database."""
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("node is required to run packages/db migrations")
    if not (DB_PACKAGE_DIR / "node_modules").exists():
        raise RuntimeError("packages/db/node_modules missing; run `pnpm install` first")
    env = {**os.environ, "DATABASE_URL": database_url}
    proc = subprocess.run(
        [node, "src/migrate.ts", "up"],
        cwd=DB_PACKAGE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"migrations failed ({proc.returncode}):\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout


UPSERT_PRODUCT = """
INSERT INTO products (sku, brand, category_path, price, currency, colour, size, attributes)
VALUES (%(sku)s, %(brand)s, category_ltree(%(category_path)s), %(price)s, %(currency)s, %(colour)s, %(size)s,
        %(attributes)s::jsonb)
ON CONFLICT (sku) DO UPDATE SET
  brand = EXCLUDED.brand, category_path = EXCLUDED.category_path, price = EXCLUDED.price,
  currency = EXCLUDED.currency, colour = EXCLUDED.colour, size = EXCLUDED.size,
  attributes = EXCLUDED.attributes
"""

UPSERT_TEXT = """
INSERT INTO product_texts (sku, lang, title, description) VALUES (%s, %s, %s, %s)
ON CONFLICT (sku, lang) DO UPDATE SET title = EXCLUDED.title, description = EXCLUDED.description
"""

INSERT_OUTBOX = """
INSERT INTO outbox (aggregate_id, event_type, payload, trace_context)
VALUES (%s, 'product.upserted', product_document(%s), %s::jsonb)
RETURNING id
"""


def _params(p: Product) -> dict[str, Any]:
    return {
        "sku": p.sku,
        "brand": p.brand,
        "category_path": p.category_path,
        "price": p.price,
        "currency": p.currency,
        "colour": p.colour,
        "size": p.size,
        "attributes": json.dumps(p.attributes, ensure_ascii=False),
    }


def upsert_products(
    database_url: str, products: Iterable[Product], trace_context: dict[str, str] | None = None
) -> list[int]:
    """Upsert products and enqueue their outbox rows atomically (one transaction per call)."""
    import psycopg

    outbox_ids: list[int] = []
    carrier = json.dumps(trace_context or {})
    with psycopg.connect(database_url) as conn, conn.transaction():
        for p in products:
            conn.execute(UPSERT_PRODUCT, _params(p))
            for lang, title, description in (
                ("en", p.title_en, p.description_en),
                ("ar", p.title_ar, p.description_ar),
            ):
                if title:
                    conn.execute(UPSERT_TEXT, (p.sku, lang, title, description))
            row = conn.execute(INSERT_OUTBOX, (p.sku, p.sku, carrier)).fetchone()
            assert row is not None
            outbox_ids.append(int(row[0]))
    return outbox_ids


def product_versions(database_url: str, skus: Iterable[str]) -> dict[str, int]:
    import psycopg

    with psycopg.connect(database_url) as conn:
        rows = conn.execute("SELECT sku, version FROM products WHERE sku = ANY(%s)", (list(skus),)).fetchall()
    return {sku: int(v) for sku, v in rows}


def unpublished_outbox_count(database_url: str) -> int:
    import psycopg

    with psycopg.connect(database_url) as conn:
        row = conn.execute("SELECT count(*) FROM outbox WHERE published_at IS NULL").fetchone()
    return int(row[0]) if row else 0


def schema_present(database_url: str) -> bool:
    """True when the release schema is already migrated (external services in CI migrate up front)."""
    import psycopg

    with psycopg.connect(database_url) as conn:
        row = conn.execute(
            "SELECT to_regclass('public.products') IS NOT NULL AND to_regclass('public.outbox') IS NOT NULL"
        ).fetchone()
    return bool(row and row[0])

from __future__ import annotations

from typing import Any

import pytest

from souq_scripts.seed.esci import attributes_for, clean_text, load_postgres, shape_product

RAW = [
    {"product_id": "B000TEST01", "product_title": "Sony WH-1000XM5 Wireless Headphones, Black",
     "product_description": "<p>Industry&nbsp;leading <b>noise</b> cancelling</p>",
     "product_bullet_point": None,
     "product_brand": "Sony", "product_color": "Midnight Black", "product_locale": "us"},
    {"product_id": "B000TEST02", "product_title": "Mens Polo Shirt Medium", "product_description": None,
     "product_bullet_point": "100% cotton\nRegular fit", "product_brand": "", "product_color": None,
     "product_locale": "us"},
]  # fmt: skip


def test_shape_product() -> None:
    a, b = (shape_product(r, 1) for r in RAW)
    assert a
    assert b
    assert a["category_ltree"] == "electronics.audio.headphones"
    assert a["colour"] == "black"
    assert a["description_en"] == "Industry leading noise cancelling"
    assert a["size"] is None
    assert a["currency"] == "AED"
    assert a["price"] > 0
    assert b["category_path"] == "Fashion > Clothing > Shirts & Tops"
    assert b["size"] == "M"
    assert b["brand"] is None
    assert b["description_en"] == "100% cotton; Regular fit"
    assert attributes_for(b, include_size=True) == {"source": "esci", "material": "cotton", "size": "M"}
    assert shape_product({**RAW[0], "product_title": "  "}, 1) is None


def test_clean_text_truncates_on_a_word_boundary() -> None:
    out = clean_text("word " * 1000, limit=50)
    assert out is not None
    assert len(out) <= 51
    assert out.endswith("…")
    assert "wor…" not in out


@pytest.mark.integration
@pytest.mark.parametrize("expanded", [False, True])
def test_load_postgres_is_idempotent_in_both_schema_phases(
    scratch_db: str, expanded: bool, run_migrations: Any, pre_expand: str
) -> None:
    import psycopg

    run_migrations(scratch_db, "up", *([] if expanded else [pre_expand]))
    products = [p for r in RAW if (p := shape_product(r, 1))]
    assert load_postgres(products, scratch_db) == 2
    with psycopg.connect(scratch_db) as conn:
        v1 = conn.execute("SELECT sku, version FROM products ORDER BY sku").fetchall()
        attrs = conn.execute("SELECT attributes FROM products WHERE sku = 'B000TEST02'").fetchone()[0]
        doc = conn.execute("SELECT product_document('B000TEST02')").fetchone()[0]
    # Old writer shape before the expand migration, typed column (mirrored by the trigger) after it.
    assert attrs["size"] == "M"
    assert doc["size"] == "M"
    assert doc["category_levels"][-1] == "fashion.clothing.shirts_and_tops"
    assert doc["title_en"] == "Mens Polo Shirt Medium"
    load_postgres(products, scratch_db)
    with psycopg.connect(scratch_db) as conn:
        assert conn.execute("SELECT sku, version FROM products ORDER BY sku").fetchall() == v1

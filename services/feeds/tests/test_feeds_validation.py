"""Row validation: every CLAUDE.md reject code, normalisation, and parity with the sample manifests."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from feeds_env import SAMPLES

from souq_feeds.importer import RowSorter
from souq_feeds.parsing import ParsedRow, iter_rows
from souq_feeds.validation import REJECT_CODES, RowRejection, category_slug, validate_row

GOOD: dict[str, Any] = {
    "sku": "SUP0000001",
    "title_en": " Apple Laptop ",
    "title_ar": "حاسوب محمول",
    "description_en": "",
    "description_ar": None,
    "brand": "Apple",
    "category_path": "Electronics >Computers>  Laptops",
    "price": "3499.5",
    "currency": "aed",
    "colour": "Silver",
    "size": "",
    "attributes": '{"model": "EI-5260"}',
}


def reject(**overrides: Any) -> RowRejection:
    with pytest.raises(RowRejection) as exc:
        validate_row({**GOOD, **overrides})
    return exc.value


def test_valid_row_is_normalised() -> None:
    row = validate_row(GOOD)
    assert row.title_en == "Apple Laptop"
    assert row.description_en is None
    assert row.price == Decimal("3499.50")
    assert row.currency == "AED"
    assert row.colour == "silver"
    assert row.size is None
    assert row.category_path == "Electronics > Computers > Laptops"
    assert row.attributes == {"model": "EI-5260"}


def test_jsonl_native_types_are_accepted() -> None:
    row = validate_row({**GOOD, "price": 19.99, "attributes": {"a": [1, 2]}, "sku": "A1"})
    assert row.price == Decimal("19.99")
    assert row.attributes == {"a": [1, 2]}


@pytest.mark.parametrize(
    ("overrides", "code", "field"),
    [
        ({"sku": ""}, "missing_field", "sku"),
        ({"brand": "  "}, "missing_field", "brand"),
        ({"category_path": None}, "missing_field", "category_path"),
        ({"price": ""}, "missing_field", "price"),
        ({"currency": ""}, "missing_field", "currency"),
        ({"title_en": "", "title_ar": ""}, "missing_field", "title_en"),
        ({"price": "abc"}, "invalid_price", "price"),
        ({"price": "-15.00"}, "invalid_price", "price"),
        ({"price": "0"}, "invalid_price", "price"),
        ({"price": "NaN"}, "invalid_price", "price"),
        ({"price": "12.3.4"}, "invalid_price", "price"),
        ({"price": "100000000000.00"}, "invalid_price", "price"),
        ({"price": "1.999"}, "invalid_price", "price"),
        ({"price": True}, "invalid_price", "price"),
        ({"currency": "US$"}, "invalid_currency", "currency"),
        ({"currency": "EURO"}, "invalid_currency", "currency"),
        ({"currency": "XYZ"}, "invalid_currency", "currency"),
        ({"attributes": "{'a': 1}"}, "invalid_json", "attributes"),
        ({"attributes": "[1, 2, 3]"}, "invalid_json", "attributes"),
        ({"attributes": '"just a string"'}, "invalid_json", "attributes"),
        ({"attributes": [1]}, "invalid_json", "attributes"),
        ({"category_path": "Electronics >  > Headphones"}, "invalid_category", "category_path"),
        ({"category_path": "!!! > ???"}, "invalid_category", "category_path"),
        ({"category_path": " > ".join("ABCDEFGHIJK")}, "invalid_category", "category_path"),
        ({"sku": "S" * 65}, "too_long", "sku"),
        ({"title_en": "t" * 2500}, "too_long", "title_en"),
        ({"description_ar": "d" * 40_000}, "too_long", "description_ar"),
        ({"brand": "b" * 1000}, "too_long", "brand"),
        ({"attributes": json.dumps({"k": "v" * 20_000})}, "too_long", "attributes"),
        ({"sku": "bad sku!"}, "malformed_row", "sku"),
    ],
)
def test_reject_codes(overrides: dict[str, Any], code: str, field: str) -> None:
    rej = reject(**overrides)
    assert rej.code == code
    assert rej.field == field
    assert rej.code in REJECT_CODES


def test_most_fundamental_problem_wins() -> None:
    rej = reject(sku="", price="abc", currency="XYZ")
    assert rej.code == "missing_field"
    assert "+2 more" in rej.message


def test_absent_jsonl_keys_are_missing_fields() -> None:
    data = {k: v for k, v in GOOD.items() if k != "currency"}
    with pytest.raises(RowRejection) as exc:
        validate_row(data)
    assert (exc.value.code, exc.value.field) == ("missing_field", "currency")


def test_category_slug_mirrors_sql_category_ltree() -> None:
    assert category_slug("Clothing, Shoes & Jewelry > Men > Shoes") == "clothing_shoes_and_jewelry.men.shoes"
    assert category_slug("Electronics >") is None
    assert category_slug(">") is None
    assert category_slug("ملابس") is None  # ASCII-only labels, as in the SQL function


def test_duplicates_keep_the_first_valid_occurrence() -> None:
    sorter = RowSorter()
    bad_first = ParsedRow(1, {**GOOD, "price": "abc"}, "")
    good = ParsedRow(2, dict(GOOD), "")
    dup = ParsedRow(3, dict(GOOD), "")
    assert sorter.accept(bad_first)[1] is not None
    assert sorter.accept(good)[0] is not None
    _, err = sorter.accept(dup)
    assert err is not None
    assert (err.row_number, err.code) == (3, "duplicate_sku_in_feed")


@pytest.mark.parametrize("fmt", ["csv", "jsonl"])
def test_sample_feed_rejects_match_the_manifest_exactly(fmt: str) -> None:
    manifest = json.loads((SAMPLES / "feed_sample_1k.manifest.json").read_text())
    sorter = RowSorter()
    total, ok, rejects = 0, 0, []
    for parsed in iter_rows(SAMPLES / f"feed_sample_1k.{fmt}", fmt):
        total += 1
        row, err = sorter.accept(parsed)
        if err is not None:
            rejects.append({"row_number": err.row_number, "code": err.code, "field": err.field})
        elif row is not None:
            ok += 1
    assert total == manifest["expected"]["rows_total"]
    assert ok == manifest["expected"]["rows_ok"]
    assert rejects == manifest["rejects"]

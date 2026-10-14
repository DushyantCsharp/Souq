from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pytest

from souq_scripts.category import slugify_category
from souq_scripts.paths import SAMPLES_DIR
from souq_scripts.seed.feedgen import FIELDS, REJECT_CODES, FeedGenerator, write_feed

# Limits the generator promises to exceed by >= 3x (see feedgen docstring).
LIMITS = {"sku": 64, "title_en": 500, "title_ar": 500, "description_en": 10_000, "description_ar": 10_000,
          "brand": 200}  # fmt: skip


def reference_validate(rec: dict[str, Any] | None, seen: set[str]) -> str | None:
    """Independent validator for the CLAUDE.md feed contract; returns the first reject code or None."""
    if rec is None:
        return "malformed_row"
    for f in ("sku", "brand", "category_path", "price", "currency"):
        if rec.get(f) in (None, ""):
            return "missing_field"
    if not rec.get("title_en") and not rec.get("title_ar"):
        return "missing_field"
    for f, limit in LIMITS.items():
        if len(str(rec.get(f) or "")) > limit:
            return "too_long"
    try:
        price = Decimal(str(rec["price"]))
        if not price.is_finite() or price < 0 or price >= Decimal("1e10") or price != round(price, 2):
            return "invalid_price"
    except InvalidOperation:
        return "invalid_price"
    if not (isinstance(rec["currency"], str) and len(rec["currency"]) == 3 and rec["currency"].isascii()
            and rec["currency"].isalpha() and rec["currency"].isupper()):  # fmt: skip
        return "invalid_currency"
    attrs = rec.get("attributes")
    if isinstance(attrs, str):
        try:
            attrs = json.loads(attrs) if attrs else {}
        except json.JSONDecodeError:
            return "invalid_json"
    if not isinstance(attrs, dict):
        return "invalid_json"
    if slugify_category(rec["category_path"]) is None:
        return "invalid_category"
    if rec["sku"] in seen:
        return "duplicate_sku_in_feed"
    seen.add(rec["sku"])
    return None


def parse_csv(text: str) -> list[dict[str, Any] | None]:
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    assert tuple(header) == FIELDS
    return [dict(zip(header, row, strict=True)) if len(row) == len(header) else None for row in reader]


def parse_jsonl(text: str) -> list[dict[str, Any] | None]:
    out: list[dict[str, Any] | None] = []
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            out.append(None)
            continue
        out.append(rec if isinstance(rec, dict) else None)
    return out


@pytest.fixture(scope="module")
def feed(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Any]]:
    out = tmp_path_factory.mktemp("feed")
    rows = FeedGenerator(5000, seed=11).generate()
    return out, write_feed(rows, out, "feed", 11, 0.01)


def test_exactly_one_percent_malformed_spread_over_all_codes(feed: tuple[Path, dict[str, Any]]) -> None:
    _, manifest = feed
    exp = manifest["expected"]
    assert exp["rows_total"] == 5000
    assert exp["rows_rejected"] == 50
    counts = exp["error_counts"]
    assert set(counts) == set(REJECT_CODES)
    # 50 over 8 codes: every code present and as even as possible.
    assert min(counts.values()) == math.floor(50 / 8)
    assert max(counts.values()) == math.ceil(50 / 8)


@pytest.mark.parametrize("fmt", ["csv", "jsonl"])
def test_each_bad_row_trips_exactly_its_code(feed: tuple[Path, dict[str, Any]], fmt: str) -> None:
    out, manifest = feed
    text = (out / f"feed.{fmt}").read_text(encoding="utf-8")
    records = parse_csv(text) if fmt == "csv" else parse_jsonl(text)
    assert len(records) == 5000
    seen: set[str] = set()
    got = {i: code for i, rec in enumerate(records, 1) if (code := reference_validate(rec, seen))}
    expected = {r["row_number"]: r["code"] for r in manifest["rejects"]}
    assert got == expected


def test_deterministic_for_a_seed(tmp_path: Path) -> None:
    def digest(seed: int, sub: str) -> str:
        d = tmp_path / sub
        write_feed(FeedGenerator(800, seed=seed).generate(), d, "f", seed, 0.01)
        return hashlib.sha256((d / "f.csv").read_bytes() + (d / "f.jsonl").read_bytes()).hexdigest()

    assert digest(3, "a") == digest(3, "b")
    assert digest(3, "a") != digest(4, "c")


def test_csv_and_jsonl_carry_the_same_valid_rows(feed: tuple[Path, dict[str, Any]]) -> None:
    out, manifest = feed
    bad = {r["row_number"] for r in manifest["rejects"]}
    csv_rows = parse_csv((out / "feed.csv").read_text(encoding="utf-8"))
    jl_rows = parse_jsonl((out / "feed.jsonl").read_text(encoding="utf-8"))
    for i, (c, j) in enumerate(zip(csv_rows, jl_rows, strict=True), 1):
        if i in bad:
            continue
        assert c is not None
        assert j is not None
        assert c["sku"] == j["sku"]
        assert c["title_en"] == j.get("title_en", "")
        assert Decimal(c["price"]) == Decimal(str(j["price"]))
        assert json.loads(c["attributes"]) == j["attributes"]


def test_committed_sample_is_current(tmp_path: Path) -> None:
    """scripts/seed/samples/feed_sample_1k.* must be what `souq-feedgen --sample` produces today."""
    write_feed(FeedGenerator(1000).generate(), tmp_path, "feed_sample_1k", 7, 0.01)
    for suffix in (".csv", ".jsonl", ".manifest.json"):
        committed = (SAMPLES_DIR / f"feed_sample_1k{suffix}").read_bytes()
        assert committed == (tmp_path / f"feed_sample_1k{suffix}").read_bytes(), f"regenerate {suffix}"

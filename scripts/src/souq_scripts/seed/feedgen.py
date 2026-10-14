"""Synthetic supplier feed generator (CSV + JSONL) in the CLAUDE.md feed row format.

Deterministic for a given (seed, rows): the same logical rows are written to ``<name>.csv`` and
``<name>.jsonl``, with exactly ``round(rows * malformed_ratio)`` rejected rows spread evenly over the eight
reject codes. Each bad row is built to trip exactly one code, and ``<name>.manifest.json`` lists every
expected reject (1-based data row number, code, field) so importer tests can assert on exact counts.

Row numbering: 1-based data rows (CSV header excluded; JSONL line number).
Format notes for importers:
  * CSV ``attributes`` is a JSON object string; JSONL ``attributes`` is an object, or a string that must
    parse to an object (an unparseable string or a non-object is ``invalid_json``).
  * JSONL ``price`` is a number, or a string that must parse as a decimal.
  * ``too_long`` rows exceed generous limits by >= 3x (title 2,500 chars, description 40,000, brand 1,000,
    sku 200); any limit at or below sku 64 / title 500 / description 10,000 / brand 200 flags them.
  * ``invalid_price`` includes a value that only overflows numeric(12,2) (>= 1e10).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import random
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from faker import Faker

from souq_scripts.paths import FEEDS_DIR, SAMPLES_DIR
from souq_scripts.prices import CURRENCY, price_for
from souq_scripts.taxonomy import APPAREL_SIZES, COLOURS, SHOE_SIZES, TAXONOMY, Category

FIELDS: tuple[str, ...] = (
    "sku", "title_en", "title_ar", "description_en", "description_ar", "brand", "category_path", "price",
    "currency", "colour", "size", "attributes",
)  # fmt: skip
REJECT_CODES: tuple[str, ...] = (
    "missing_field", "invalid_price", "invalid_currency", "invalid_json", "invalid_category", "too_long",
    "duplicate_sku_in_feed", "malformed_row",
)  # fmt: skip
DEFAULT_SEED = 7
DEFAULT_RATIO = 0.01

_ADJ_EN = ("Premium", "Classic", "Compact", "Pro", "Lightweight", "Essential", "Deluxe", "Everyday", "Ultra")
_FEATURES_EN = (
    "1-year local warranty", "Free delivery across the UAE", "Ships in 24 hours", "Genuine product",
    "Easy returns within 15 days", "Imported quality", "Best seller in its category",
)  # fmt: skip
_FEATURES_AR = (
    "ضمان محلي لمدة سنة", "توصيل مجاني داخل الإمارات", "شحن خلال 24 ساعة", "منتج أصلي",
    "إرجاع سهل خلال 15 يوماً", "جودة مستوردة", "الأكثر مبيعاً في فئته",
)  # fmt: skip
_BAD_PRICES = ("abc", "-15.00", "12.3.4", "NaN", "100000000000.00", "free")
_BAD_CURRENCIES = ("US$", "EURO", "DIRHAM", "A1D", "AE", "€")
_BAD_JSON = ('{"material": "cotton",', "{material: cotton}", "[1, 2, 3]", '"just a string"', "{'a': 1}")
_BAD_CATEGORIES = (
    "Electronics >  > Headphones", ">", "!!! > ???", "Electronics >",
    "A > B > C > D > E > F > G > H > I > J > K",
)  # fmt: skip
_MISSING = ("sku", "title", "brand", "category_path", "price", "currency")
_TOO_LONG = ("title_en", "title_ar", "description_en", "brand", "sku")


@dataclass
class Row:
    number: int
    values: dict[str, Any]
    code: str | None = None
    field: str | None = None
    csv_line: str | None = None  # pre-rendered (malformed CSV only)
    jsonl_line: str | None = None  # pre-rendered (malformed JSON only)


class FeedGenerator:
    def __init__(self, rows: int, seed: int = DEFAULT_SEED, malformed_ratio: float = DEFAULT_RATIO) -> None:
        if rows < 2:
            raise ValueError("need at least 2 rows")
        self.rows = rows
        self.seed = seed
        self.rng = random.Random(seed)
        self.fake = Faker()
        self.fake.seed_instance(seed)
        n_bad = round(rows * malformed_ratio)
        # Row 1 is always valid so every duplicate has an earlier original.
        positions = sorted(self.rng.sample(range(2, rows + 1), n_bad))
        codes = [REJECT_CODES[i % len(REJECT_CODES)] for i in range(n_bad)]
        self.rng.shuffle(codes)
        self.bad: dict[int, str] = dict(zip(positions, codes, strict=True))

    def sku(self, n: int) -> str:
        return f"SUP{n:07d}"

    def valid_row(self, n: int) -> dict[str, Any]:
        rng = self.rng
        cat: Category = rng.choice(TAXONOMY)
        brand = rng.choice(cat.brands) if cat.brands else self.fake.company().split()[0]
        model = self.fake.bothify("??-####").upper()
        colour = rng.choice(list(COLOURS)) if rng.random() < 0.9 else None
        colour_ar = COLOURS[colour][0] if colour else None
        size: str | None = None
        if cat.size_kind == "apparel":
            size = rng.choice(APPAREL_SIZES)
        elif cat.size_kind == "shoe":
            size = rng.choice(SHOE_SIZES)
        sku = self.sku(n)
        title_en = f"{brand} {rng.choice(_ADJ_EN)} {cat.noun_en} {model}" + (
            f", {colour.title()}" if colour else ""
        )
        title_ar = f"{cat.noun_ar} {brand} {model}" + (f" - {colour_ar}" if colour_ar else "")
        feat = rng.randrange(len(_FEATURES_EN))
        desc_en = f"{brand} {cat.noun_en.lower()} ({model}). {_FEATURES_EN[feat]}."
        desc_ar = f"{cat.noun_ar} من {brand} ({model}). {_FEATURES_AR[feat]}."
        which = rng.random()
        if which < 0.05:  # English-only listing
            title_ar, desc_ar = "", ""
        elif which < 0.07:  # Arabic-only listing
            title_en, desc_en = "", ""
        attrs: dict[str, Any] = {"model": model}
        if cat.materials:
            attrs["material"] = rng.choice(cat.materials)
        if cat.path.startswith("Electronics"):
            attrs["warranty_months"] = rng.choice((6, 12, 24))
        return {
            "sku": sku,
            "title_en": title_en,
            "title_ar": title_ar,
            "description_en": desc_en,
            "description_ar": desc_ar,
            "brand": brand,
            "category_path": cat.path,
            "price": price_for(cat, sku, self.seed),
            "currency": CURRENCY,
            "colour": colour or "",
            "size": size or "",
            "attributes": attrs,
        }

    def corrupt(self, row: Row, earlier_valid: list[int], valid_skus: dict[int, str]) -> None:
        rng, v = self.rng, row.values
        code = row.code
        if code == "missing_field":
            field = rng.choice(_MISSING)
            row.field = "title_en" if field == "title" else field
            for f in ("title_en", "title_ar") if field == "title" else (field,):
                v[f] = ""
        elif code == "invalid_price":
            row.field, v["price"] = "price", rng.choice(_BAD_PRICES)
        elif code == "invalid_currency":
            row.field, v["currency"] = "currency", rng.choice(_BAD_CURRENCIES)
        elif code == "invalid_json":
            row.field, v["attributes"] = "attributes", rng.choice(_BAD_JSON)
        elif code == "invalid_category":
            row.field, v["category_path"] = "category_path", rng.choice(_BAD_CATEGORIES)
        elif code == "too_long":
            field = rng.choice(_TOO_LONG)
            row.field = field
            if field == "sku":
                v["sku"] = v["sku"] + "-" + "X" * 189
            elif field == "brand":
                v["brand"] = ("Brand " * 200)[:1000]
            elif field.startswith("title"):
                if not v[field]:  # the listing had no text in this language; make it exist and be long
                    v[field] = v["title_en"] or v["title_ar"]
                v[field] = (v[field] + " ") * (2500 // max(1, len(v[field])) + 1)
                v[field] = v[field][:2500]
            else:
                v[field] = ((v[field] or "Lorem ipsum dolor sit amet.") + " ") * 2000
                v[field] = v[field][:40000]
        elif code == "duplicate_sku_in_feed":
            row.field = "sku"
            v["sku"] = valid_skus[rng.choice(earlier_valid)]
        elif code == "malformed_row":
            row.field = None
            short = [str(v["sku"]), v["title_en"], v["brand"]]
            if rng.random() < 0.5:
                row.csv_line = _csv_line(short)  # 3 of 12 columns
            else:
                row.csv_line = _csv_line([*_csv_values(v), "extra", "columns"])  # 14 of 12
            row.jsonl_line = rng.choice(
                (
                    json.dumps({"sku": v["sku"], "title_en": v["title_en"]}, ensure_ascii=False)[:-12],
                    json.dumps([v["sku"], v["title_en"]], ensure_ascii=False),
                    "not json at all",
                )
            )

    def generate(self) -> list[Row]:
        out: list[Row] = []
        valid_numbers: list[int] = []
        valid_skus: dict[int, str] = {}
        for n in range(1, self.rows + 1):
            row = Row(n, self.valid_row(n), self.bad.get(n))
            if row.code:
                self.corrupt(row, valid_numbers, valid_skus)
            else:
                valid_numbers.append(n)
                valid_skus[n] = row.values["sku"]
            out.append(row)
        return out


def _csv_values(v: dict[str, Any]) -> list[str]:
    out = []
    for f in FIELDS:
        x = v[f]
        if f == "attributes" and isinstance(x, dict):
            x = json.dumps(x, ensure_ascii=False, sort_keys=True)
        elif f == "price" and isinstance(x, float):
            x = f"{x:.2f}"
        out.append("" if x is None else str(x))
    return out


def _csv_line(values: list[str]) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerow(values)
    return buf.getvalue()


def _jsonl_line(v: dict[str, Any]) -> str:
    obj = {}
    for f in FIELDS:
        x = v[f]
        if x == "" and f != "attributes":
            continue  # missing / empty optional fields are omitted in JSONL
        obj[f] = x
    return json.dumps(obj, ensure_ascii=False, sort_keys=False) + "\n"


def write_feed(rows: list[Row], out_dir: Path, name: str, seed: int, ratio: float) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (
        (out_dir / f"{name}.csv").open("w", encoding="utf-8", newline="") as csv_fh,
        (out_dir / f"{name}.jsonl").open("w", encoding="utf-8") as jl_fh,
    ):
        csv_fh.write(_csv_line(list(FIELDS)))
        for r in rows:
            csv_fh.write(r.csv_line or _csv_line(_csv_values(r.values)))
            jl_fh.write((r.jsonl_line + "\n") if r.jsonl_line else _jsonl_line(r.values))
    counts = Counter(r.code for r in rows if r.code)
    rejected = sum(counts.values())
    manifest = {
        "name": name,
        "seed": seed,
        "malformed_ratio": ratio,
        "row_numbering": "1-based data rows (CSV header excluded; JSONL line number)",
        "files": [f"{name}.csv", f"{name}.jsonl"],
        "expected": {
            "rows_total": len(rows),
            "rows_ok": len(rows) - rejected,
            "rows_rejected": rejected,
            "error_counts": {c: counts.get(c, 0) for c in REJECT_CODES},
        },
        "rejects": [{"row_number": r.number, "code": r.code, "field": r.field} for r in rows if r.code],
    }
    (out_dir / f"{name}.manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rows", type=int, default=500_000)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--malformed-ratio", type=float, default=DEFAULT_RATIO)
    ap.add_argument("--out-dir", default=str(FEEDS_DIR))
    ap.add_argument("--name", default=None, help="file stem (default supplier_<rows/1000>k)")
    ap.add_argument("--sample", action="store_true", help="write the committed 1k test sample instead")
    args = ap.parse_args(argv)
    if args.sample:
        args.rows, args.out_dir, args.name = 1000, str(SAMPLES_DIR), "feed_sample_1k"
    name = args.name or (
        f"supplier_{args.rows // 1000}k" if args.rows % 1000 == 0 else f"supplier_{args.rows}"
    )
    rows = FeedGenerator(args.rows, args.seed, args.malformed_ratio).generate()
    manifest = write_feed(rows, Path(args.out_dir), name, args.seed, args.malformed_ratio)
    exp = manifest["expected"]
    print(
        f"[feedgen] {args.out_dir}/{name}.{{csv,jsonl}}: {exp['rows_total']} rows, "
        f"{exp['rows_rejected']} malformed {exp['error_counts']}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()

"""Deterministic bilingual product factories, supplier feed rows and catalog.changes events.

Everything is a pure function of an integer seed so a failing integration run can be replayed exactly and
two tests never fight over randomly generated SKUs.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

FEED_COLUMNS = (
    "sku",
    "title_en",
    "title_ar",
    "description_en",
    "description_ar",
    "brand",
    "category_path",
    "price",
    "currency",
    "colour",
    "size",
    "attributes",
)

COLOURS = ("black", "white", "red", "blue", "green")
COLOURS_AR = {"black": "اسود", "white": "ابيض", "red": "احمر", "blue": "ازرق", "green": "اخضر"}


@dataclass(frozen=True)
class Template:
    category_path: str
    brands: tuple[str, ...]
    title_en: str
    title_ar: str
    description_en: str
    description_ar: str
    base_price: int
    sizes: tuple[str, ...] = ()
    currency: str = "AED"


TEMPLATES: tuple[Template, ...] = (
    Template(
        "Electronics > Audio > Headphones",
        ("sony", "bose", "jbl"),
        "{brand} wireless noise cancelling headphones",
        "سماعات {brand} لاسلكية بخاصية إلغاء الضوضاء",
        "Over-ear bluetooth headphones with 30 hour battery life.",
        "سماعات رأس بلوتوث مع بطارية تدوم 30 ساعة.",
        400,
    ),
    Template(
        "Clothing > Shoes > Running",
        ("nike", "adidas", "puma"),
        "{brand} men's running shoes",
        "حذاء جري رجالي من {brand}",
        "Lightweight breathable running shoes with cushioned sole.",
        "حذاء جري خفيف الوزن يسمح بمرور الهواء مع نعل مريح.",
        250,
        sizes=("40", "41", "42", "43", "44", "45"),
    ),
    Template(
        "Bags > Backpacks",
        ("samsonite", "nike", "adidas"),
        "{brand} laptop backpack",
        "حقيبة ظهر للابتوب من {brand}",
        "Water resistant backpack with a padded 15 inch laptop sleeve.",
        "حقيبة ظهر مقاومة للماء مع جيب مبطن للابتوب 15 بوصة.",
        180,
    ),
    Template(
        "Home > Kitchen > Coffee Makers",
        ("delonghi", "philips"),
        "{brand} espresso coffee machine",
        "ماكينة قهوة اسبريسو من {brand}",
        "15 bar pump espresso machine with milk frother.",
        "ماكينة اسبريسو بمضخة 15 بار مع مبخرة حليب.",
        900,
    ),
    Template(
        "Electronics > Wearables > Smartwatches",
        ("apple", "samsung", "huawei"),
        "{brand} smart watch with heart rate monitor",
        "ساعة ذكية من {brand} مع مراقب نبض القلب",
        "Fitness tracking smartwatch with GPS and sleep monitoring.",
        "ساعة ذكية لتتبع اللياقة مع نظام تحديد المواقع ومراقبة النوم.",
        1200,
        currency="SAR",
    ),
    Template(
        "Electronics > Accessories > Phone Cases",
        ("spigen", "otterbox"),
        "{brand} iphone 15 case",
        "جراب ايفون 15 من {brand}",
        "Shock absorbing slim case with raised camera lip.",
        "جراب رفيع ممتص للصدمات مع حافة مرتفعة للكاميرا.",
        90,
    ),
)


@dataclass(frozen=True)
class Product:
    sku: str
    title_en: str | None
    title_ar: str | None
    description_en: str | None
    description_ar: str | None
    brand: str
    category_path: str
    price: Decimal
    currency: str
    colour: str | None
    size: str | None
    attributes: dict[str, Any] = field(default_factory=dict)

    def with_(self, **changes: Any) -> Product:
        return replace(self, **changes)


def sku_for(n: int, prefix: str = "T") -> str:
    return f"{prefix}{n:07d}"


def make_product(n: int, *, prefix: str = "T", **overrides: Any) -> Product:
    t = TEMPLATES[n % len(TEMPLATES)]
    brand = t.brands[(n // len(TEMPLATES)) % len(t.brands)]
    colour = COLOURS[(n // 2) % len(COLOURS)]
    size = t.sizes[n % len(t.sizes)] if t.sizes else None
    price = Decimal(t.base_price + (n * 37) % 200) + Decimal("0.99")
    attributes: dict[str, Any] = {"material": "synthetic"} if t.sizes else {"warranty_months": 12}
    p = Product(
        sku=sku_for(n, prefix),
        title_en=f"{t.title_en.format(brand=brand.title())} {colour}",
        title_ar=f"{t.title_ar.format(brand=brand.title())} {COLOURS_AR[colour]}",
        description_en=t.description_en,
        description_ar=t.description_ar,
        brand=brand,
        category_path=t.category_path,
        price=price,
        currency=t.currency,
        colour=colour,
        size=size,
        attributes=attributes,
    )
    return replace(p, **overrides) if overrides else p


def catalog(count: int, *, start: int = 0, prefix: str = "T", **overrides: Any) -> list[Product]:
    return [make_product(n, prefix=prefix, **overrides) for n in range(start, start + count)]


def tagged_catalog(count: int, token: str, *, start: int = 0, prefix: str = "T") -> list[Product]:
    """Products whose titles all carry ``token`` so a query for it has a known, closed result set."""
    out = []
    for p in catalog(count, start=start, prefix=prefix):
        out.append(
            p.with_(
                title_en=f"{token} {p.title_en}",
                title_ar=f"{token} {p.title_ar}",
                description_en=f"{p.description_en} {token} edition.",
            )
        )
    return out


# --------------------------------------------------------------------------- category slug


def slugify_category(path: str) -> str | None:
    """Python mirror of the ``category_ltree()`` SQL function (packages/db) for hand-built events."""
    parts = path.split(">")
    if not path.strip() or len(parts) > 10:
        return None
    labels = []
    for part in parts:
        label = part.strip().translate(
            str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
        )
        label = label.replace("&", " and ")
        label = re.sub(r"[^a-z0-9]+", "_", label).strip("_")
        if not label or len(label) > 255:
            return None
        labels.append(label)
    return ".".join(labels)


def category_levels(slug: str) -> list[str]:
    parts = slug.split(".")
    return [".".join(parts[: i + 1]) for i in range(len(parts))]


# --------------------------------------------------------------------------- feed rows


def feed_row(p: Product) -> dict[str, Any]:
    """Supplier feed row (JSONL shape: attributes stays an object, price a string like suppliers send)."""
    return {
        "sku": p.sku,
        "title_en": p.title_en,
        "title_ar": p.title_ar,
        "description_en": p.description_en,
        "description_ar": p.description_ar,
        "brand": p.brand,
        "category_path": p.category_path,
        "price": str(p.price),
        "currency": p.currency,
        "colour": p.colour,
        "size": p.size,
        "attributes": p.attributes,
    }


def _csv_cell(key: str, value: Any) -> str:
    if value is None:
        return ""
    if key == "attributes" and not isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def to_csv(rows: Iterable[Mapping[str, Any]], columns: tuple[str, ...] = FEED_COLUMNS) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_csv_cell(c, row.get(c)) for c in columns])
    return buf.getvalue()


def to_jsonl(rows: Iterable[Mapping[str, Any]]) -> str:
    return "".join(json.dumps(dict(r), ensure_ascii=False, sort_keys=True) + "\n" for r in rows)


def write_feed(path: Path, products: Iterable[Product], fmt: str = "csv") -> Path:
    rows = [feed_row(p) for p in products]
    path.write_text(to_csv(rows) if fmt == "csv" else to_jsonl(rows), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- catalog.changes events


def product_document(p: Product, version: int, updated_at: datetime | None = None) -> dict[str, Any]:
    """Same shape as the ``product_document(sku)`` SQL function, for events produced straight to Kafka."""
    slug = slugify_category(p.category_path)
    if slug is None:
        raise ValueError(f"category {p.category_path!r} has no ltree form")
    ts = (updated_at or datetime.now(UTC)).isoformat()
    attributes = dict(p.attributes)
    if p.size is not None:
        attributes["size"] = p.size
    return {
        "sku": p.sku,
        "brand": p.brand,
        "category_path": slug,
        "category_levels": category_levels(slug),
        "price": float(p.price),
        "currency": p.currency,
        "colour": p.colour,
        "size": p.size,
        "attributes": attributes,
        "title_en": p.title_en,
        "title_ar": p.title_ar,
        "description_en": p.description_en,
        "description_ar": p.description_ar,
        "updated_at": ts,
        "version": version,
    }


def change_event(
    p: Product, version: int, *, event_id: str, event_type: str = "product.upserted"
) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "event_id": event_id,
        "event_type": event_type,
        "sku": p.sku,
        "version": version,
        "outbox_created_at": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "product": product_document(p, version, now) if event_type == "product.upserted" else None,
    }


def event_headers(event: Mapping[str, Any], attempt: int = 0) -> list[tuple[str, bytes]]:
    return [
        ("event_id", str(event["event_id"]).encode()),
        ("sku", str(event["sku"]).encode()),
        ("version", str(event["version"]).encode()),
        ("attempt", str(attempt).encode()),
    ]


# --------------------------------------------------------------------------- bad feed rows


def bad_feed_rows() -> Iterator[tuple[str, dict[str, Any]]]:
    """(expected reject code, row) pairs; one unambiguous trigger per code in the feeds contract."""
    base = feed_row(make_product(900_001, prefix="BAD"))

    def row(**changes: Any) -> dict[str, Any]:
        r = dict(base)
        r.update(changes)
        return r

    yield "missing_field", row(sku="BAD-MISSING-BRAND", brand="")
    yield "missing_field", row(sku="BAD-MISSING-TITLES", title_en="", title_ar="")
    yield "invalid_price", row(sku="BAD-PRICE-TEXT", price="cheap")
    yield "invalid_price", row(sku="BAD-PRICE-NEG", price="-5.00")
    yield "invalid_currency", row(sku="BAD-CURRENCY", currency="DIRHAM")
    yield "invalid_json", row(sku="BAD-JSON", attributes="{not json")
    yield "invalid_category", row(sku="BAD-CATEGORY", category_path="إلكترونيات > صوتيات")
    yield "too_long", row(sku="BAD-LONG-" + "X" * 200)


def as_dict(p: Product) -> dict[str, Any]:
    return asdict(p)

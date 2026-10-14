"""Seed the catalog from the Amazon Shopping Queries (ESCI) dataset.

Picks US-locale queries from the ESCI ``small_version`` test split in a seeded random order until the
union of their judged products reaches ``--target-products`` (~50k), so every judged product of every
selected query is in the catalog (the golden set never points at a missing SKU). Products get a category
(keyword rules, ESCI has none), a canonical colour, a size for apparel/shoes and a Faker AED price.

Outputs (schemas are a contract with the search agent's eval harness, see docs/handoffs/search.md):
  data/esci/products.parquet, data/esci/queries.parquet, data/esci/judgments.parquet
and upserts products + product_texts(en) into Postgres. Seeds deliberately write no outbox rows: the
indices are filled by ``scripts/reindex.py`` straight from Postgres.

Raw files are downloaded once into data/raw/esci/ (resumable, time-boxed).
"""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import random
import re
import sys
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from souq_scripts.db import connect
from souq_scripts.paths import ESCI_DIR, RAW_DIR
from souq_scripts.prices import CURRENCY, DEFAULT_SEED, price_for
from souq_scripts.taxonomy import (
    APPAREL_SIZES,
    SHOE_SIZES,
    Category,
    canonical_colour,
    classify,
    extract_size,
)

ESCI_BASE = "https://media.githubusercontent.com/media/amazon-science/esci-data/main/shopping_queries_dataset"
EXAMPLES_FILE = "shopping_queries_dataset_examples.parquet"
PRODUCTS_FILE = "shopping_queries_dataset_products.parquet"
GAINS: dict[str, int] = {"E": 3, "S": 2, "C": 1, "I": 0}
MAX_DESCRIPTION = 2000

PRODUCTS_SCHEMA = pa.schema(
    [
        ("sku", pa.string()),
        ("title_en", pa.string()),
        ("description_en", pa.string()),
        ("bullet_points", pa.string()),
        ("brand", pa.string()),
        ("colour_raw", pa.string()),
        ("colour", pa.string()),
        ("size", pa.string()),
        ("category_path", pa.string()),
        ("category_ltree", pa.string()),
        ("price", pa.float64()),
        ("currency", pa.string()),
        ("locale", pa.string()),
        ("title_ar", pa.string()),
        ("title_ar_provider", pa.string()),
    ]
)
QUERIES_SCHEMA = pa.schema(
    [
        ("query_id", pa.int64()),
        ("query", pa.string()),
        ("split", pa.string()),
        ("n_judged", pa.int32()),
        ("n_exact", pa.int32()),
    ]
)
JUDGMENTS_SCHEMA = pa.schema(
    [("query_id", pa.int64()), ("sku", pa.string()), ("esci_label", pa.string()), ("gain", pa.int8())]
)

_MATERIALS = (
    "stainless steel", "cast iron", "cotton", "leather", "wood", "plastic", "silicone", "glass", "ceramic",
    "polyester", "wool", "bamboo", "aluminum", "metal", "nylon", "denim", "linen", "silk",
)  # fmt: skip
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def log(msg: str) -> None:
    print(f"[esci] {msg}", file=sys.stderr, flush=True)


# --- download ----------------------------------------------------------------------------------------


def download(url: str, dest: Path, deadline: float) -> None:
    """Resumable download into ``dest`` (via ``dest.part`` + HTTP Range); raises TimeoutError at deadline."""
    if dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    with httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, read=60.0)) as client:
        total = int(client.head(url).headers.get("content-length", 0))
        have = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        with client.stream("GET", url, headers=headers) as resp:
            resp.raise_for_status()
            if have and resp.status_code != 206:
                have = 0  # server ignored Range: start over
            with part.open("ab" if have else "wb") as fh:
                last = time.monotonic()
                for chunk in resp.iter_bytes(1 << 20):
                    fh.write(chunk)
                    have += len(chunk)
                    if time.monotonic() - last > 15:
                        last = time.monotonic()
                        log(f"{dest.name}: {have / 1e6:.0f}/{total / 1e6:.0f} MB")
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"download time box hit; resume later ({part}, {have} bytes)")
    if total and part.stat().st_size != total:
        raise OSError(f"{dest.name}: got {part.stat().st_size} bytes, expected {total}")
    part.rename(dest)


def ensure_raw(raw_dir: Path, timeout_s: float) -> tuple[Path, Path]:
    deadline = time.monotonic() + timeout_s
    paths = []
    for name in (EXAMPLES_FILE, PRODUCTS_FILE):
        path = raw_dir / name
        download(f"{ESCI_BASE}/{name}", path, deadline)
        paths.append(path)
    return paths[0], paths[1]


# --- selection ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Judgment:
    query_id: int
    sku: str
    label: str


@dataclass(frozen=True)
class Query:
    query_id: int
    query: str
    split: str


def load_examples(path: Path, split: str | None) -> tuple[dict[int, Query], dict[int, list[Judgment]]]:
    t = pq.read_table(
        path,
        columns=["query_id", "query", "product_id", "product_locale", "esci_label", "small_version", "split"],
    )
    mask = pc.and_(pc.equal(t["product_locale"], "us"), pc.equal(t["small_version"], 1))
    if split:
        mask = pc.and_(mask, pc.equal(t["split"], split))
    t = t.filter(mask)
    queries: dict[int, Query] = {}
    judged: dict[int, list[Judgment]] = {}
    for qid, q, pid, label, sp in zip(
        t["query_id"].to_pylist(),
        t["query"].to_pylist(),
        t["product_id"].to_pylist(),
        t["esci_label"].to_pylist(),
        t["split"].to_pylist(),
        strict=True,
    ):
        queries.setdefault(qid, Query(qid, q, sp))
        judged.setdefault(qid, []).append(Judgment(qid, pid, label))
    return queries, judged


def select_queries(judged: dict[int, list[Judgment]], target_products: int, seed: int) -> list[int]:
    """Seeded random queries until the union of their judged products reaches the target."""
    qids = sorted(judged)
    random.Random(seed).shuffle(qids)
    chosen: list[int] = []
    products: set[str] = set()
    for qid in qids:
        if len(products) >= target_products:
            break
        chosen.append(qid)
        products.update(j.sku for j in judged[qid])
    return chosen


def iter_raw_products(path: Path, wanted: set[str]) -> Iterator[dict[str, Any]]:
    """Stream the 1.1 GB products file in batches, keeping only wanted US products."""
    wanted_arr = pa.array(sorted(wanted), pa.string())
    pf = pq.ParquetFile(path)
    cols = ["product_id", "product_title", "product_description", "product_bullet_point", "product_brand",
            "product_color", "product_locale"]  # fmt: skip
    for batch in pf.iter_batches(batch_size=100_000, columns=cols):
        mask = pc.and_(pc.equal(batch["product_locale"], "us"), pc.is_in(batch["product_id"], wanted_arr))
        yield from pa.Table.from_batches([batch]).filter(mask).to_pylist()


# --- product shaping ---------------------------------------------------------------------------------


def clean_text(raw: str | None, limit: int = MAX_DESCRIPTION) -> str | None:
    if not raw:
        return None
    s = _WS.sub(" ", html.unescape(_TAG.sub(" ", raw))).strip()
    if not s:
        return None
    if len(s) > limit:
        cut = s.rfind(" ", 0, limit)
        s = s[: cut if cut > limit // 2 else limit].rstrip(" ,;.") + "…"
    return s


def _stable_pick(options: tuple[str, ...], key: str) -> str:
    h = int.from_bytes(hashlib.blake2b(key.encode(), digest_size=4).digest(), "big")
    return options[h % len(options)]


def size_for(category: Category, title: str, sku: str) -> str | None:
    """Title-stated size, else a stable synthetic one for apparel/shoes (ESCI has no size field)."""
    stated = extract_size(title, category.size_kind)
    if stated:
        return stated
    if category.size_kind == "apparel":
        return _stable_pick(APPAREL_SIZES, sku)
    if category.size_kind == "shoe":
        return _stable_pick(SHOE_SIZES, sku)
    return None


def material_for(title: str) -> str | None:
    t = title.lower()
    for m in _MATERIALS:
        if re.search(rf"(?<![a-z]){re.escape(m)}(?![a-z])", t):
            return m
    return None


def shape_product(raw: dict[str, Any], price_seed: int) -> dict[str, Any] | None:
    title = clean_text(raw.get("product_title"), 500)
    if not title:
        return None
    sku: str = raw["product_id"]
    category = classify(title)
    description = clean_text(raw.get("product_description"))
    bullets = raw.get("product_bullet_point")
    if not description and bullets:
        description = clean_text("; ".join(b.strip() for b in bullets.splitlines() if b.strip()))
    colour_raw = (raw.get("product_color") or "").strip() or None
    brand = (raw.get("product_brand") or "").strip() or None
    return {
        "sku": sku,
        "title_en": title,
        "description_en": description,
        "bullet_points": bullets,
        "brand": brand[:200] if brand else None,
        "colour_raw": colour_raw,
        "colour": canonical_colour(colour_raw) or canonical_colour(title),
        "size": size_for(category, title, sku),
        "category_path": category.path,
        "category_ltree": category.ltree,
        "price": price_for(category, sku, price_seed),
        "currency": CURRENCY,
        "locale": "us",
        "title_ar": None,
        "title_ar_provider": None,
        "_material": material_for(f"{title} {description or ''}"),
    }


def attributes_for(p: dict[str, Any], include_size: bool) -> dict[str, Any]:
    attrs: dict[str, Any] = {"source": "esci"}
    if p.get("_material"):
        attrs["material"] = p["_material"]
    if include_size and p.get("size"):
        attrs["size"] = p["size"]
    return attrs


# --- outputs -----------------------------------------------------------------------------------------


def write_outputs(
    out_dir: Path,
    products: list[dict[str, Any]],
    queries: list[Query],
    judgments: list[Judgment],
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = [{k: p[k] for k in PRODUCTS_SCHEMA.names} for p in products]
    pq.write_table(pa.Table.from_pylist(rows, PRODUCTS_SCHEMA), out_dir / "products.parquet")
    by_q: dict[int, list[Judgment]] = {}
    for j in judgments:
        by_q.setdefault(j.query_id, []).append(j)
    qrows = [
        {
            "query_id": q.query_id,
            "query": q.query,
            "split": q.split,
            "n_judged": len(by_q[q.query_id]),
            "n_exact": sum(1 for j in by_q[q.query_id] if j.label == "E"),
        }
        for q in queries
    ]
    pq.write_table(pa.Table.from_pylist(qrows, QUERIES_SCHEMA), out_dir / "queries.parquet")
    jrows = [
        {"query_id": j.query_id, "sku": j.sku, "esci_label": j.label, "gain": GAINS[j.label]}
        for j in judgments
    ]
    pq.write_table(pa.Table.from_pylist(jrows, JUDGMENTS_SCHEMA), out_dir / "judgments.parquet")


def _copy_rows(rows: Iterable[tuple[Any, ...]]) -> io.StringIO:
    """Rows as COPY text format (tab-separated, \\N for NULL, backslash-escaped)."""
    buf = io.StringIO()
    for row in rows:
        out = []
        for v in row:
            if v is None:
                out.append("\\N")
            else:
                s = str(v)
                out.append(
                    s.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")
                )
        buf.write("\t".join(out) + "\n")
    buf.seek(0)
    return buf


def products_has_size_column(conn: Any) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name = 'products' AND column_name = 'size'"
    )
    return cur.fetchone() is not None


def load_postgres(products: list[dict[str, Any]], database_url: str | None) -> int:
    """Upsert products + English texts. Idempotent: unchanged rows keep their version (DB trigger)."""
    with connect(database_url) as conn:
        # Before the expand migration the only home for size is attributes.size (the "old writer" shape);
        # after it, write the typed column like any new writer (the sync trigger mirrors it back).
        typed_size = products_has_size_column(conn)
        conn.execute(
            """CREATE TEMP TABLE stage_products (sku text, brand text, category_path text, price numeric,
                 currency text, colour text, size text, attributes jsonb, title_en text, description_en text)
               ON COMMIT DROP"""
        )
        rows = (
            (
                p["sku"], p["brand"], p["category_path"], p["price"], p["currency"], p["colour"],
                p["size"] if typed_size else None, json.dumps(attributes_for(p, include_size=not typed_size)),
                p["title_en"], p["description_en"],
            )
            for p in products
        )  # fmt: skip
        with conn.cursor() as cur, cur.copy("COPY stage_products FROM STDIN") as copy:
            copy.write(_copy_rows(rows).getvalue())
        size_cols = ("size, ", "s.size, ", "size = EXCLUDED.size, ") if typed_size else ("", "", "")
        conn.execute(
            f"""INSERT INTO products (sku, brand, category_path, price, currency, colour,
                                      {size_cols[0]}attributes)
                SELECT s.sku, s.brand, category_ltree(s.category_path), s.price, s.currency, s.colour,
                       {size_cols[1]}s.attributes
                  FROM stage_products s
                ON CONFLICT (sku) DO UPDATE SET brand = EXCLUDED.brand,
                       category_path = EXCLUDED.category_path,
                       price = EXCLUDED.price, currency = EXCLUDED.currency, colour = EXCLUDED.colour,
                       {size_cols[2]}attributes = EXCLUDED.attributes"""
        )
        conn.execute(
            """INSERT INTO product_texts (sku, lang, title, description)
               SELECT sku, 'en', title_en, description_en FROM stage_products
               ON CONFLICT (sku, lang) DO UPDATE
                  SET title = EXCLUDED.title, description = EXCLUDED.description"""
        )
        n = conn.execute("SELECT count(*) FROM stage_products").fetchone()
        conn.commit()
    return int(n[0]) if n else 0


# --- CLI ---------------------------------------------------------------------------------------------


def run(args: argparse.Namespace) -> None:
    examples_path, products_path = ensure_raw(Path(args.raw_dir), args.download_timeout_s)
    queries, judged = load_examples(examples_path, args.split or None)
    log(f"{len(queries)} US small-version queries in split={args.split or 'all'}")
    chosen = select_queries(judged, args.target_products, args.seed)
    wanted = {j.sku for q in chosen for j in judged[q]}
    log(f"selected {len(chosen)} queries covering {len(wanted)} products; reading products file")

    products: dict[str, dict[str, Any]] = {}
    for raw in iter_raw_products(products_path, wanted):
        shaped = shape_product(raw, args.price_seed)
        if shaped:
            products[shaped["sku"]] = shaped
    # Full coverage: a query whose judged products are not all usable is dropped, never half-kept.
    kept = [q for q in chosen if all(j.sku in products for j in judged[q])]
    if len(kept) < len(chosen):
        log(f"dropped {len(chosen) - len(kept)} queries with missing/empty-title products")
    kept_skus = {j.sku for q in kept for j in judged[q]}
    product_rows = sorted((p for s, p in products.items() if s in kept_skus), key=lambda p: p["sku"])
    query_rows = sorted((queries[q] for q in kept), key=lambda q: q.query_id)
    judgment_rows = sorted((j for q in kept for j in judged[q]), key=lambda j: (j.query_id, j.sku))

    write_outputs(Path(args.out_dir), product_rows, query_rows, judgment_rows)
    log(f"wrote {len(product_rows)} products, {len(query_rows)} queries, {len(judgment_rows)} judgments")
    if not args.no_load:
        n = load_postgres(product_rows, args.database_url)
        log(f"upserted {n} products into Postgres")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target-products", type=int, default=50_000)
    ap.add_argument("--seed", type=int, default=13, help="query sampling seed")
    ap.add_argument("--price-seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--split", default="test", help="ESCI split to sample queries from ('' for all)")
    ap.add_argument("--raw-dir", default=str(RAW_DIR / "esci"))
    ap.add_argument("--out-dir", default=str(ESCI_DIR))
    ap.add_argument("--download-timeout-s", type=float, default=1800)
    ap.add_argument("--database-url", default=None, help="defaults to DATABASE_URL / souq settings")
    ap.add_argument("--no-load", action="store_true", help="write parquet only, skip Postgres")
    run(ap.parse_args(argv))


if __name__ == "__main__":
    main()

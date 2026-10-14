"""Static supplier-feed fixtures in ``tests/fixtures/feeds/`` generated from the factories.

Regenerate after changing the factories: ``python tests/fixtures/souq_testing/feed_files.py``.
The files are committed so feeds tests, demos and the chaos suite can use them without importing Python.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from souq_testing.factories import FEED_COLUMNS, bad_feed_rows, catalog, feed_row, to_csv, to_jsonl
from souq_testing.paths import FEEDS_FIXTURES_DIR


def bad_rows_csv() -> tuple[str, list[dict[str, object]]]:
    """CSV text with one row per reject code (plus a good row) and the expected per-row outcome."""
    good = feed_row(catalog(1, start=900_100, prefix="OK")[0])
    rows = [good]
    expected: list[dict[str, object]] = []
    for code, row in bad_feed_rows():
        rows.append(row)
        expected.append({"row_number": len(rows), "code": code, "sku": row["sku"]})
    # The same SKU twice in one feed: the second occurrence is rejected.
    rows.append(dict(good))
    expected.append({"row_number": len(rows), "code": "duplicate_sku_in_feed", "sku": good["sku"]})
    text = to_csv(rows)
    # A row with the wrong number of columns cannot be mapped to the header at all.
    text += "MALFORMED-1,only,three\n"
    expected.append({"row_number": len(rows) + 1, "code": "malformed_row", "sku": "MALFORMED-1"})
    return text, expected


def write_all(out_dir: Path = FEEDS_FIXTURES_DIR) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    products = catalog(60, prefix="FX")
    rows = [feed_row(p) for p in products]
    written = []
    for name, content in (
        ("catalog_small.csv", to_csv(rows)),
        ("catalog_small.jsonl", to_jsonl(rows)),
    ):
        path = out_dir / name
        path.write_text(content, encoding="utf-8")
        written.append(path)
    text, expected = bad_rows_csv()
    (out_dir / "bad_rows.csv").write_text(text, encoding="utf-8")
    (out_dir / "bad_rows.expected.json").write_text(
        json.dumps(
            {"columns": list(FEED_COLUMNS), "rows_total": expected[-1]["row_number"], "rejects": expected},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    written += [out_dir / "bad_rows.csv", out_dir / "bad_rows.expected.json"]
    return written


if __name__ == "__main__":
    for path in write_all():
        print(path)

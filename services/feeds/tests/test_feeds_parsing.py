"""Streaming CSV/JSONL parsing and structural-failure detection."""

from __future__ import annotations

import codecs
import gzip
from pathlib import Path

import pytest

from souq_feeds.parsing import StructuralError, iter_rows

HEADER = (
    "sku,title_en,title_ar,description_en,description_ar,brand,category_path,price,currency,colour,size,"
    "attributes\n"
)
ROW = 'A1,Thing,,,,Acme,Home > Kitchen,10.00,AED,red,,"{""k"": 1}"\n'


def write(tmp_path: Path, name: str, data: bytes | str) -> Path:
    p = tmp_path / name
    p.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    return p


def test_csv_rows_are_numbered_from_one_excluding_the_header(tmp_path: Path) -> None:
    p = write(tmp_path, "f.csv", HEADER + ROW + "\n" + ROW.replace("A1", "A2"))
    rows = list(iter_rows(p, "csv"))
    assert [r.row_number for r in rows] == [1, 2]  # blank line skipped
    assert rows[0].data is not None
    assert rows[0].data["attributes"] == '{"k": 1}'
    assert rows[0].raw.startswith("A1,Thing")


def test_csv_wrong_field_count_is_malformed_row(tmp_path: Path) -> None:
    p = write(tmp_path, "f.csv", HEADER + "A1,Thing,Acme\n" + ROW)
    first, second = iter_rows(p, "csv")
    assert first.error is not None
    assert first.error.code == "malformed_row"
    assert second.error is None


def test_csv_header_is_case_and_bom_insensitive(tmp_path: Path) -> None:
    p = write(tmp_path, "f.csv", codecs.BOM_UTF8 + (HEADER.upper() + ROW).encode())
    [row] = iter_rows(p, "csv")
    assert row.data is not None
    assert row.data["sku"] == "A1"


@pytest.mark.parametrize(
    ("header", "message"),
    [
        ("sku,title_en,brand,category_path,price\n", "currency"),
        ("sku,brand,category_path,price,currency\n", "title_en|title_ar"),
        ("sku;title_en;brand;category_path;price;currency\n", "missing required header"),
        ("sku,sku,title_en,brand,category_path,price,currency\n", "duplicate header"),
    ],
)
def test_missing_or_ambiguous_header_is_structural(tmp_path: Path, header: str, message: str) -> None:
    p = write(tmp_path, "f.csv", header + "x\n")
    with pytest.raises(StructuralError, match=message.replace("|", r"\|")):
        list(iter_rows(p, "csv"))


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "empty"),
        (gzip.compress(b"sku\n"), "gzip"),
        (b"PK\x03\x04rest", "zip"),
        (codecs.BOM_UTF16_LE + "sku".encode("utf-16-le"), "UTF-16"),
        ("sku,title_en".encode("utf-16-le"), "NUL"),
        (HEADER.encode() + "A1,\xe9t\xe9\n".encode("latin-1"), "UTF-8"),
    ],
)
def test_unreadable_files_are_structural(tmp_path: Path, data: bytes, message: str) -> None:
    p = write(tmp_path, "f.csv", data)
    with pytest.raises(StructuralError, match=message):
        list(iter_rows(p, "csv"))


def test_bad_utf8_after_the_sniff_window_aborts_mid_stream(tmp_path: Path) -> None:
    body = HEADER + ROW * 20_000  # > 1 MiB of valid rows first
    p = write(tmp_path, "f.csv", body.encode() + b"A9,\xff\xfe,,,,Acme,Home,1,AED,,,\n")
    rows = iter_rows(p, "csv")
    assert next(rows).row_number == 1  # rows before the bad bytes are delivered (and get committed)
    with pytest.raises(StructuralError, match="UTF-8"):
        list(rows)


def test_jsonl_lines_and_malformed_lines(tmp_path: Path) -> None:
    lines = [
        '{"sku": "A1", "price": 10}',
        "",
        "not json at all",
        '["A2", "x"]',
        '{"SKU": "A3"}',
    ]
    p = write(tmp_path, "f.jsonl", "\n".join(lines) + "\n")
    rows = list(iter_rows(p, "jsonl"))
    assert [r.row_number for r in rows] == [1, 3, 4, 5]  # JSONL uses physical line numbers
    assert rows[0].data == {"sku": "A1", "price": 10}
    assert [r.error.code if r.error else None for r in rows] == [None, "malformed_row", "malformed_row", None]
    assert rows[3].data == {"sku": "A3"}  # keys are case-normalised like CSV headers


def test_json_array_file_is_not_jsonl(tmp_path: Path) -> None:
    p = write(tmp_path, "f.jsonl", '[\n  {"sku": "A1"}\n]\n')
    with pytest.raises(StructuralError, match="JSON array"):
        list(iter_rows(p, "jsonl"))


def test_unknown_format_is_structural(tmp_path: Path) -> None:
    p = write(tmp_path, "f.xml", "<feed/>")
    with pytest.raises(StructuralError, match="unsupported format"):
        list(iter_rows(p, "xml"))

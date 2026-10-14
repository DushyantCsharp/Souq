"""Streaming CSV / JSONL readers. Neither ever holds more than one record in memory.

Row numbers are 1-based *data* row numbers: the CSV header is not counted; for JSONL the physical line
number is used (blank lines are skipped but still advance the count). This matches the feed manifests
produced by scripts/ (``feed_row_errors.row_number``).
"""

from __future__ import annotations

import codecs
import csv
import io
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from souq_feeds.validation import REQUIRED_COLUMNS, TITLE_COLUMNS, RowRejection

SNIFF_BYTES = 1024 * 1024
MAX_JSONL_LINE = 1024 * 1024
MAX_RAW_CHARS = 8192

# Large enough that an over-long description is reported as too_long by validation rather than as a
# csv.Error (malformed_row), small enough to bound memory for a single pathological field.
csv.field_size_limit(4 * 1024 * 1024)


class StructuralError(Exception):
    """The file as a whole cannot be imported (encoding, header, format). Aborts the feed."""


@dataclass(frozen=True, slots=True)
class ParsedRow:
    row_number: int
    data: dict[str, Any] | None
    raw: str
    error: RowRejection | None = None


def sniff(path: Path, fmt: str) -> None:
    """Fail fast, before importing anything, on files that are clearly not UTF-8 CSV/JSONL."""
    with path.open("rb") as fh:
        head = fh.read(SNIFF_BYTES)
    if not head:
        raise StructuralError("file is empty")
    if head.startswith(b"\x1f\x8b"):
        raise StructuralError("unsupported format: gzip-compressed file (upload it uncompressed)")
    if head.startswith(b"PK\x03\x04"):
        raise StructuralError("unsupported format: zip/xlsx archive (export as UTF-8 CSV or JSONL)")
    if head.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        raise StructuralError("unreadable encoding: UTF-32 (re-export as UTF-8)")
    if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        raise StructuralError("unreadable encoding: UTF-16 (re-export as UTF-8)")
    if b"\x00" in head:
        raise StructuralError("unreadable encoding: NUL bytes (UTF-16 without BOM or binary file)")
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    try:
        text = decoder.decode(head, final=len(head) < SNIFF_BYTES)
    except UnicodeDecodeError as exc:
        raise StructuralError(f"unreadable encoding: not valid UTF-8 at byte {exc.start}") from None
    if fmt == "jsonl" and text.lstrip("﻿ \t\r\n").startswith("["):
        raise StructuralError("unsupported format: a JSON array is not JSONL (one object per line)")


def _truncate(text: str) -> str:
    return text if len(text) <= MAX_RAW_CHARS else text[:MAX_RAW_CHARS] + "…"


def _csv_line(record: list[str]) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="").writerow(record)
    return _truncate(buf.getvalue())


def read_csv_header(path: Path) -> list[str]:
    with path.open(encoding="utf-8-sig", newline="", errors="strict") as fh:
        try:
            header = next(csv.reader(fh))
        except StopIteration:
            raise StructuralError("file is empty") from None
        except UnicodeDecodeError as exc:
            raise StructuralError(f"unreadable encoding: not valid UTF-8 at byte {exc.start}") from None
        except csv.Error as exc:
            raise StructuralError(f"unreadable CSV header: {exc}") from None
    return [h.strip().lower() for h in header]


def check_header(columns: list[str]) -> None:
    missing = [c for c in REQUIRED_COLUMNS if c not in columns]
    if not any(c in columns for c in TITLE_COLUMNS):
        missing.append("title_en|title_ar")
    if missing:
        raise StructuralError(f"missing required header column(s): {', '.join(missing)}")
    dupes = sorted({c for c in columns if c and columns.count(c) > 1})
    if dupes:
        raise StructuralError(f"duplicate header column(s): {', '.join(dupes)}")


def iter_csv(path: Path) -> Iterator[ParsedRow]:
    columns = read_csv_header(path)
    check_header(columns)
    width = len(columns)
    with path.open(encoding="utf-8-sig", newline="", errors="strict") as fh:
        reader = csv.reader(fh, strict=False)
        n = 0
        try:
            next(reader)  # header, validated above
            while True:
                try:
                    record = next(reader)
                except StopIteration:
                    return
                except csv.Error as exc:
                    n += 1
                    yield ParsedRow(
                        n, None, "", RowRejection("malformed_row", None, f"CSV parse error: {exc}")
                    )
                    continue
                if not record or (len(record) == 1 and not record[0].strip()):
                    continue  # blank line
                n += 1
                if len(record) != width:
                    yield ParsedRow(
                        n,
                        None,
                        _csv_line(record),
                        RowRejection("malformed_row", None, f"expected {width} fields, found {len(record)}"),
                    )
                    continue
                yield ParsedRow(n, dict(zip(columns, record, strict=True)), _csv_line(record))
        except UnicodeDecodeError as exc:
            raise StructuralError(
                f"unreadable encoding: not valid UTF-8 after data row {n} (byte {exc.start} of a read chunk)"
            ) from None


def iter_jsonl(path: Path) -> Iterator[ParsedRow]:
    with path.open("rb") as fh:
        line_no = 0
        while True:
            chunk = fh.readline(MAX_JSONL_LINE)
            if not chunk:
                return
            line_no += 1
            if len(chunk) == MAX_JSONL_LINE and not chunk.endswith(b"\n"):
                # Over-long line: skip the remainder without buffering it.
                while (rest := fh.readline(MAX_JSONL_LINE)) and not rest.endswith(b"\n"):
                    pass
                yield ParsedRow(
                    line_no,
                    None,
                    "",
                    RowRejection("malformed_row", None, f"line exceeds {MAX_JSONL_LINE} bytes"),
                )
                continue
            try:
                line = chunk.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise StructuralError(
                    f"unreadable encoding: line {line_no} is not valid UTF-8 (byte {exc.start})"
                ) from None
            if line_no == 1:
                line = line.lstrip("﻿")
            stripped = line.strip()
            if not stripped:
                continue
            raw = _truncate(stripped)
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError as exc:
                yield ParsedRow(
                    line_no, None, raw, RowRejection("malformed_row", None, f"invalid JSON: {exc.msg}")
                )
                continue
            if not isinstance(obj, dict):
                yield ParsedRow(
                    line_no, None, raw, RowRejection("malformed_row", None, "line is not a JSON object")
                )
                continue
            yield ParsedRow(line_no, {str(k).strip().lower(): v for k, v in obj.items()}, raw)


def iter_rows(path: Path, fmt: str) -> Iterator[ParsedRow]:
    sniff(path, fmt)
    if fmt == "csv":
        return iter_csv(path)
    if fmt == "jsonl":
        return iter_jsonl(path)
    raise StructuralError(f"unsupported format: {fmt!r} (expected csv or jsonl)")

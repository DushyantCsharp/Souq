"""``souq-feeds-import FILE [--format csv|jsonl]`` — run an import in-process, without the HTTP API.

Used for throughput measurement and by the chaos test; it creates a normal ``feed_imports`` row, so the
result is visible through ``GET /feeds/{id}`` like any upload.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import uuid
from pathlib import Path

import psycopg

from souq_common.telemetry import init_telemetry
from souq_feeds.config import load_settings
from souq_feeds.importer import run_import
from souq_feeds.logs import configure_logging


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="souq-feeds-import")
    parser.add_argument("file", type=Path)
    parser.add_argument("--format", choices=["csv", "jsonl"], default=None)
    parser.add_argument("--copy", action="store_true", help="copy the file into FEEDS_UPLOAD_DIR first")
    args = parser.parse_args(argv)

    configure_logging("souq-feeds-import")
    init_telemetry("feeds")
    settings = load_settings()
    path: Path = args.file.resolve()
    fmt = args.format or {".csv": "csv", ".jsonl": "jsonl", ".ndjson": "jsonl"}.get(path.suffix.lower())
    if fmt is None:
        parser.error("cannot infer --format from the file name")
    feed_id = uuid.uuid4()
    if args.copy:
        settings.upload_dir.mkdir(parents=True, exist_ok=True)
        target = settings.upload_dir / f"{feed_id}.{fmt}"
        shutil.copyfile(path, target)
        path = target
    with psycopg.connect(
        settings.database_url, autocommit=True, application_name="souq-feeds-import"
    ) as conn:
        conn.execute(
            """
            INSERT INTO feed_imports (id, filename, format, status, byte_size, content_sha256, file_path)
            VALUES (%s, %s, %s, 'queued', %s, %s, %s)
            """,
            (feed_id, args.file.name, fmt, path.stat().st_size, _sha256(path), str(path)),
        )
        outcome = run_import(conn, feed_id, settings.batch_size)
    if outcome is None:
        print(json.dumps({"id": str(feed_id), "status": "interrupted"}))
        return 1
    c = outcome.counters
    print(
        json.dumps(
            {
                "id": str(feed_id),
                "status": outcome.status,
                "rows_total": c.total,
                "rows_ok": c.ok,
                "rows_rejected": c.rejected,
                "rows_changed": c.changed,
                "error_counts": dict(sorted(c.errors.items())),
                "seconds": round(outcome.seconds, 2),
                "rows_per_s": round(c.total / outcome.seconds, 1) if outcome.seconds else None,
                "failure_reason": outcome.failure_reason,
            }
        )
    )
    return 0 if outcome.status == "completed" else 2


if __name__ == "__main__":
    sys.exit(main())

"""Feed import against real Postgres (throwaway database with the packages/db migrations).

Needs DATABASE_URL pointing at a Postgres 16 server where the user may CREATE DATABASE; skipped otherwise.
"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from feeds_env import SAMPLES, scratch_database

from souq_common.settings import get_settings
from souq_feeds.api import create_app
from souq_feeds.config import FeedsSettings
from souq_feeds.importer import run_import

pytestmark = pytest.mark.integration

MANIFEST = json.loads((SAMPLES / "feed_sample_1k.manifest.json").read_text())


@pytest.fixture(scope="module")
def dsn() -> Iterator[str]:
    try:
        with psycopg.connect(get_settings().database_url, connect_timeout=3):
            pass
    except psycopg.Error as exc:
        pytest.skip(f"postgres not reachable: {exc}")
    with scratch_database(get_settings().database_url) as scratch:
        yield scratch


def _connect(dsn: str) -> psycopg.Connection[Any]:
    return psycopg.connect(dsn, autocommit=True)


def _import(dsn: str, path: Path, fmt: str, tmp_path: Path) -> uuid.UUID:
    feed_id = uuid.uuid4()
    staged = tmp_path / f"{feed_id}.{fmt}"
    shutil.copyfile(path, staged)
    with _connect(dsn) as conn:
        conn.execute(
            "INSERT INTO feed_imports (id, filename, format, file_path) VALUES (%s, %s, %s, %s)",
            (feed_id, path.name, fmt, str(staged)),
        )
        assert run_import(conn, feed_id) is not None
    return feed_id


def _feed(dsn: str, feed_id: uuid.UUID) -> dict[str, Any]:
    with _connect(dsn) as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        row = cur.execute("SELECT * FROM feed_imports WHERE id = %s", (feed_id,)).fetchone()
    assert row is not None
    return row


def _count(dsn: str, sql: str, *params: Any) -> int:
    with _connect(dsn) as conn:
        row = conn.execute(sql, params).fetchone()  # type: ignore[call-overload]
    return int(row[0])


def test_sample_import_matches_manifest_and_is_idempotent(dsn: str, tmp_path: Path) -> None:
    feed_id = _import(dsn, SAMPLES / "feed_sample_1k.csv", "csv", tmp_path)
    feed = _feed(dsn, feed_id)
    expected = MANIFEST["expected"]
    assert feed["status"] == "completed"
    assert feed["rows_total"] == expected["rows_total"]
    assert feed["rows_ok"] == expected["rows_ok"]
    assert feed["rows_rejected"] == expected["rows_rejected"]
    assert feed["error_counts"] == {k: v for k, v in expected["error_counts"].items() if v}
    with _connect(dsn) as conn:
        errors = conn.execute(
            "SELECT row_number, code, field FROM feed_row_errors WHERE feed_id = %s ORDER BY id", (feed_id,)
        ).fetchall()
    assert [{"row_number": r[0], "code": r[1], "field": r[2]} for r in errors] == MANIFEST["rejects"]

    ok = expected["rows_ok"]
    assert _count(dsn, "SELECT count(*) FROM products") == ok
    assert _count(dsn, "SELECT count(*) FROM outbox") == ok
    # every outbox payload is the post-write product_document, carrying the final version
    assert (
        _count(
            dsn,
            "SELECT count(*) FROM outbox o JOIN products p ON p.sku = o.aggregate_id"
            " WHERE (o.payload->>'version')::bigint = p.version AND o.payload->>'sku' = p.sku",
        )
        == ok
    )

    # the JSONL rendering of the same feed changes nothing: no version bumps, no new events
    again = _import(dsn, SAMPLES / "feed_sample_1k.jsonl", "jsonl", tmp_path)
    assert _feed(dsn, again)["rows_ok"] == ok
    assert _count(dsn, "SELECT count(*) FROM outbox") == ok


def test_effective_change_bumps_version_once_and_emits_one_event(dsn: str, tmp_path: Path) -> None:
    header = "sku,title_en,title_ar,brand,category_path,price,currency,colour,size,attributes\n"
    first = tmp_path / "a.csv"
    first.write_text(header + 'CHG1,Lamp,مصباح,Acme,Home > Lighting,10.00,AED,white,,"{}"\n')
    _import(dsn, first, "csv", tmp_path)
    with _connect(dsn) as conn:
        v1 = conn.execute("SELECT version FROM products WHERE sku = 'CHG1'").fetchone()
    second = tmp_path / "b.csv"
    second.write_text(header + 'CHG1,Lamp XL,مصباح,Acme,Home > Lighting,10.00,AED,white,,"{}"\n')
    _import(dsn, second, "csv", tmp_path)
    with _connect(dsn) as conn:
        v2 = conn.execute("SELECT version FROM products WHERE sku = 'CHG1'").fetchone()
        events = conn.execute(
            "SELECT payload->>'title_en', (payload->>'version')::int FROM outbox WHERE aggregate_id = 'CHG1'"
            " ORDER BY id"
        ).fetchall()
    assert v1 is not None
    assert v2 is not None
    assert v2[0] == v1[0] + 1
    assert events == [("Lamp", v1[0]), ("Lamp XL", v2[0])]


def test_structural_failure_marks_the_feed_failed(dsn: str, tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("sku,title_en,price\nX,Y,1\n")
    feed = _feed(dsn, _import(dsn, bad, "csv", tmp_path))
    assert feed["status"] == "failed"
    assert "missing required header" in feed["failure_reason"]


def test_http_upload_status_and_error_paging(dsn: str, tmp_path: Path) -> None:
    settings = FeedsSettings(upload_dir=tmp_path / "uploads", database_url=dsn, import_workers=1)
    with TestClient(create_app(settings)) as client:
        with (SAMPLES / "feed_sample_1k.jsonl").open("rb") as fh:
            resp = client.post("/feeds", files={"file": ("feed_sample_1k.jsonl", fh)})
        assert resp.status_code == 202
        feed_id = resp.json()["id"]
        deadline = time.monotonic() + 60
        status: dict[str, Any] = {}
        while time.monotonic() < deadline:
            status = client.get(f"/feeds/{feed_id}").json()
            if status["status"] in ("completed", "failed"):
                break
            time.sleep(0.2)
        assert status["status"] == "completed"
        assert status["rows_rejected"] == MANIFEST["expected"]["rows_rejected"]
        assert status["started_at"] is not None
        assert status["finished_at"] is not None

        items: list[dict[str, Any]] = []
        cursor = None
        pages = 0
        while True:
            params: dict[str, Any] = {"limit": 3}
            if cursor:
                params["cursor"] = cursor
            page = client.get(f"/feeds/{feed_id}/errors", params=params).json()
            items += page["items"]
            pages += 1
            cursor = page["next_cursor"]
            if not cursor:
                break
        assert pages == 4
        assert [(i["row_number"], i["code"]) for i in items] == [
            (r["row_number"], r["code"]) for r in MANIFEST["rejects"]
        ]
        assert all(i["message"] for i in items)


def test_shutdown_requeues_and_recovery_resumes(dsn: str, tmp_path: Path) -> None:
    import threading

    from psycopg_pool import ConnectionPool

    from souq_feeds.runner import ImportRunner

    feed_id = uuid.uuid4()
    staged = tmp_path / f"{feed_id}.csv"
    shutil.copyfile(SAMPLES / "feed_sample_1k.csv", staged)
    with _connect(dsn) as conn:
        conn.execute(
            "INSERT INTO feed_imports (id, filename, format, file_path) VALUES (%s, 'x.csv', 'csv', %s)",
            (feed_id, str(staged)),
        )
        stop = threading.Event()
        stop.set()  # shutdown already requested: stop after the first committed batch
        assert run_import(conn, feed_id, 100, stop) is None
    interrupted = _feed(dsn, feed_id)
    assert interrupted["status"] == "queued"
    assert 0 < interrupted["rows_total"] < 1000

    pool: ConnectionPool[Any] = ConnectionPool(
        dsn, min_size=1, max_size=2, kwargs={"autocommit": True}, open=True
    )
    runner = ImportRunner(pool, workers=1, batch_size=1000)
    try:
        assert runner.recover() >= 1
        runner._executor.shutdown(wait=True)
    finally:
        pool.close()
    done = _feed(dsn, feed_id)
    assert done["status"] == "completed"
    assert done["rows_total"] == 1000
    assert done["rows_rejected"] == MANIFEST["expected"]["rows_rejected"]
    with _connect(dsn) as conn:
        n_errors = conn.execute(
            "SELECT count(*) FROM feed_row_errors WHERE feed_id = %s", (feed_id,)
        ).fetchone()
    assert n_errors == (MANIFEST["expected"]["rows_rejected"],)  # the re-run replaced, not appended, errors

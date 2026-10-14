"""HTTP edge without a database: multipart streaming, problem+json errors, OpenAPI shape."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from souq_feeds.api import create_app, decode_cursor, encode_cursor
from souq_feeds.config import FeedsSettings
from souq_feeds.upload import MalformedUploadError, UploadTooLargeError, receive_upload


class FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def fetchone(self) -> Any:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[Any]:
        return self.rows


class FakeConn:
    def __init__(self, pool: FakePool) -> None:
        self.pool = pool

    def execute(self, query: str, params: Any = None) -> FakeResult:
        self.pool.queries.append((query, params))
        return FakeResult([])

    @contextmanager
    def cursor(self, row_factory: Any = None) -> Iterator[FakeConn]:
        yield self


class FakePool:
    def __init__(self) -> None:
        self.queries: list[tuple[str, Any]] = []

    @contextmanager
    def connection(self, timeout: float | None = None) -> Iterator[FakeConn]:
        yield FakeConn(self)

    def close(self) -> None:
        pass


class FakeRunner:
    def __init__(self) -> None:
        self.submitted: list[UUID] = []

    def submit(self, feed_id: UUID) -> None:
        self.submitted.append(feed_id)

    def recover(self) -> int:
        return 0

    def shutdown(self) -> None:
        pass


@pytest.fixture
def client(tmp_path: Path) -> Iterator[tuple[TestClient, FakePool, FakeRunner, Path]]:
    settings = FeedsSettings(upload_dir=tmp_path, max_upload_bytes=10_000, database_url="postgres://unused")
    pool, runner = FakePool(), FakeRunner()
    app = create_app(settings, pool, runner)  # type: ignore[arg-type]
    with TestClient(app) as c:
        yield c, pool, runner, tmp_path


CSV = b"sku,title_en,brand,category_path,price,currency\nA1,Thing,Acme,Home,1.00,AED\n"


def test_upload_streams_to_disk_and_queues_the_import(client: Any) -> None:
    c, pool, runner, upload_dir = client
    resp = c.post("/feeds", files={"file": ("supplier.csv", CSV, "text/csv")})
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    feed_id = UUID(body["id"])
    assert resp.headers["location"] == f"/feeds/{feed_id}"
    assert runner.submitted == [feed_id]
    assert (upload_dir / f"{feed_id}.csv").read_bytes() == CSV
    [(_, params)] = [q for q in pool.queries if "INSERT INTO feed_imports" in q[0]]
    assert params[1:5] == ("supplier.csv", "csv", len(CSV), params[4])
    assert len(params[4]) == 64  # sha256 hex


def test_format_field_overrides_the_extension(client: Any) -> None:
    c, _, _, upload_dir = client
    resp = c.post("/feeds", files={"file": ("dump.txt", b'{"sku": "A"}\n')}, data={"format": "jsonl"})
    assert resp.status_code == 202
    assert (upload_dir / f"{resp.json()['id']}.jsonl").exists()


def test_unsupported_format_is_422_problem(client: Any) -> None:
    c, _, runner, upload_dir = client
    resp = c.post("/feeds", files={"file": ("feed.xml", b"<x/>")}, data={"format": "xml"})
    assert resp.status_code == 422
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["type"].endswith("/unsupported-format")
    assert runner.submitted == []
    assert list(upload_dir.iterdir()) == []  # partial upload removed


def test_missing_file_part_is_422(client: Any) -> None:
    c, *_ = client
    resp = c.post("/feeds", data={"format": "csv"}, files={"other": ("x", b"y")})
    assert resp.status_code == 422


def test_non_multipart_body_is_415(client: Any) -> None:
    c, *_ = client
    resp = c.post("/feeds", content=CSV, headers={"content-type": "text/csv"})
    assert resp.status_code == 415
    assert resp.json()["status"] == 415


def test_oversized_upload_is_413_and_leaves_nothing_behind(client: Any) -> None:
    c, _, runner, upload_dir = client
    resp = c.post("/feeds", files={"file": ("big.csv", b"x" * 20_000)})
    assert resp.status_code == 413
    assert resp.json()["type"].endswith("/upload-too-large")
    assert list(upload_dir.iterdir()) == []
    assert runner.submitted == []


def test_unknown_feed_is_404_problem(client: Any) -> None:
    c, *_ = client
    resp = c.get(f"/feeds/{uuid4()}")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/problem+json")
    assert resp.json()["title"] == "Feed not found"
    assert c.get("/feeds/not-a-uuid").status_code == 422


def test_bad_cursor_is_400(client: Any) -> None:
    c, *_ = client
    resp = c.get(f"/feeds/{uuid4()}/errors", params={"cursor": "%%%"})
    assert resp.status_code == 400
    assert resp.json()["type"].endswith("/invalid-cursor")


def test_cursor_round_trip() -> None:
    assert decode_cursor(encode_cursor(12345)) == 12345


def test_openapi_documents_the_internal_contract(client: Any) -> None:
    c, *_ = client
    spec = c.get("/openapi.json").json()
    assert set(spec["paths"]) == {"/feeds", "/feeds/{feed_id}", "/feeds/{feed_id}/errors"}
    upload = spec["paths"]["/feeds"]["post"]
    schema = upload["requestBody"]["content"]["multipart/form-data"]["schema"]
    assert schema["required"] == ["file"]
    assert schema["properties"]["format"]["enum"] == ["csv", "jsonl"]
    assert "202" in upload["responses"]
    status = spec["components"]["schemas"]["FeedStatus"]["properties"]
    assert set(status) == {
        "id",
        "status",
        "rows_total",
        "rows_ok",
        "rows_rejected",
        "error_counts",
        "started_at",
        "finished_at",
        "failure_reason",
    }


async def _chunks(data: bytes, size: int) -> AsyncIterator[bytes]:
    for i in range(0, len(data), size):
        yield data[i : i + size]


def _multipart(boundary: str, file_bytes: bytes) -> bytes:
    return (
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="format"\r\n\r\n'
            "csv\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="f.csv"\r\n'
            "Content-Type: text/csv\r\n\r\n"
        ).encode()
        + file_bytes
        + f"\r\n--{boundary}--\r\n".encode()
    )


async def test_receiver_handles_arbitrary_chunk_boundaries(tmp_path: Path) -> None:
    payload = CSV * 50
    body = _multipart("XyZ", payload)
    for size in (1, 7, 64, 4096):
        dest = tmp_path / f"out{size}"
        got = await receive_upload(_chunks(body, size), "multipart/form-data; boundary=XyZ", dest, 1_000_000)
        assert dest.read_bytes() == payload
        assert got.fields == {"format": "csv"}
        assert got.filename == "f.csv"
        assert got.size == len(payload)


async def test_receiver_limits_and_rejects(tmp_path: Path) -> None:
    body = _multipart("B", b"x" * 1000)
    dest = tmp_path / "f"
    with pytest.raises(UploadTooLargeError):
        await receive_upload(_chunks(body, 100), "multipart/form-data; boundary=B", dest, 500)
    assert not dest.exists()
    with pytest.raises(MalformedUploadError):
        await receive_upload(_chunks(body, 100), "application/json", dest, 500)

"""Feeds internal API (CLAUDE.md "Feeds internal API"). Errors are RFC 9457 problem+json."""

from __future__ import annotations

import base64
import binascii
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

import psycopg
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field
from python_multipart.exceptions import MultipartParseError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from souq_common.telemetry import inject_carrier, instrument_fastapi
from souq_feeds.config import FeedsSettings, load_settings
from souq_feeds.runner import ImportRunner
from souq_feeds.upload import MalformedUploadError, UploadTooLargeError, receive_upload
from souq_feeds.validation import RejectCode

log = logging.getLogger(__name__)

PROBLEM_BASE = "https://souq.dev/problems/"
PROBLEM_JSON = "application/problem+json"
FeedStatusName = Literal["queued", "running", "completed", "failed"]
_EXTENSIONS = {".csv": "csv", ".jsonl": "jsonl", ".ndjson": "jsonl"}


# --------------------------------------------------------------------------- models
class FeedAccepted(BaseModel):
    id: UUID
    status: FeedStatusName


class FeedStatus(BaseModel):
    id: UUID
    status: FeedStatusName
    rows_total: int
    rows_ok: int
    rows_rejected: int
    error_counts: dict[str, int] = Field(description="reject code -> number of rows")
    started_at: datetime | None
    finished_at: datetime | None
    failure_reason: str | None


class FeedRowError(BaseModel):
    row_number: int = Field(description="1-based data row (CSV header excluded; JSONL line number)")
    code: RejectCode
    field: str | None
    message: str
    raw: str | None


class FeedErrorsPage(BaseModel):
    items: list[FeedRowError]
    next_cursor: str | None


class Problem(BaseModel):
    type: str
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None


class ProblemError(Exception):
    def __init__(self, status: int, slug: str, title: str, detail: str | None = None) -> None:
        super().__init__(detail or title)
        self.status = status
        self.slug = slug
        self.title = title
        self.detail = detail


def problem_response(
    request: Request, status: int, slug: str, title: str, detail: str | None
) -> JSONResponse:
    body = Problem(
        type=PROBLEM_BASE + slug, title=title, status=status, detail=detail, instance=request.url.path
    )
    return JSONResponse(body.model_dump(exclude_none=True), status_code=status, media_type=PROBLEM_JSON)


def _problem_responses(*codes: int) -> dict[int | str, dict[str, Any]]:
    return {c: {"model": Problem, "content": {PROBLEM_JSON: {}}} for c in codes}


# --------------------------------------------------------------------------- cursor
def encode_cursor(last_id: int) -> str:
    return base64.urlsafe_b64encode(f"e:{last_id}".encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> int:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        prefix, _, value = raw.partition(":")
        if prefix != "e":
            raise ValueError(cursor)
        last = int(value)
        if last < 0:
            raise ValueError(cursor)
        return last
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise ProblemError(
            400, "invalid-cursor", "Invalid cursor", "cursor is not one this API issued"
        ) from None


# --------------------------------------------------------------------------- app
UPLOAD_SCHEMA: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["file"],
                    "properties": {
                        "file": {"type": "string", "format": "binary", "description": "CSV or JSONL feed"},
                        "format": {
                            "type": "string",
                            "enum": ["csv", "jsonl"],
                            "description": "Defaults from the file extension (.csv, .jsonl/.ndjson)",
                        },
                    },
                }
            }
        },
    }
}


def create_app(
    settings: FeedsSettings | None = None,
    pool: ConnectionPool[Any] | None = None,
    runner: ImportRunner | None = None,
) -> FastAPI:
    cfg = settings or load_settings()
    owns_pool = pool is None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal pool, runner
        cfg.upload_dir.mkdir(parents=True, exist_ok=True)
        if pool is None:
            pool = ConnectionPool(
                cfg.database_url,
                min_size=1,
                max_size=cfg.pool_max,
                kwargs={"autocommit": True, "application_name": "souq-feeds"},
                open=False,
            )
            pool.open(wait=False)
        if runner is None:
            runner = ImportRunner(pool, cfg.import_workers, cfg.batch_size)
        app.state.pool = pool
        app.state.runner = runner
        try:
            await run_in_threadpool(runner.recover)
        except psycopg.Error as exc:
            log.warning("could not check for interrupted imports: %s", exc)
        yield
        await run_in_threadpool(runner.shutdown)
        if owns_pool:
            pool.close()

    app = FastAPI(
        title="Souq feeds (internal)",
        version="1.0.0",
        description="Supplier feed upload and streaming import. Internal: called by the gateway only.",
        lifespan=lifespan,
    )
    instrument_fastapi(app)

    @app.exception_handler(ProblemError)
    async def _problem(request: Request, exc: ProblemError) -> JSONResponse:
        return problem_response(request, exc.status, exc.slug, exc.title, exc.detail)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        slug = "not-found" if exc.status_code == 404 else f"http-{exc.status_code}"
        return problem_response(request, exc.status_code, slug, str(exc.detail), None)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        detail = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        return problem_response(request, 422, "validation-error", "Request validation failed", detail)

    @app.exception_handler(psycopg.OperationalError)
    async def _db_down(request: Request, exc: psycopg.OperationalError) -> JSONResponse:
        return problem_response(request, 503, "database-unavailable", "Database unavailable", None)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def readyz(request: Request) -> JSONResponse:
        try:
            with request.app.state.pool.connection(timeout=2) as conn:
                conn.execute("SELECT 1")
        except Exception as exc:
            return JSONResponse({"status": "unavailable", "detail": str(exc)}, status_code=503)
        return JSONResponse({"status": "ok"})

    @app.post(
        "/feeds",
        status_code=202,
        response_model=FeedAccepted,
        openapi_extra=UPLOAD_SCHEMA,
        responses=_problem_responses(400, 413, 415, 422),
        summary="Upload a supplier feed; the import runs in the background",
    )
    async def upload_feed(request: Request) -> JSONResponse:
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > cfg.max_upload_bytes + 64 * 1024:
            raise ProblemError(
                413, "upload-too-large", "Upload too large", f"limit is {cfg.max_upload_bytes} bytes"
            )
        feed_id = uuid4()
        part_path = cfg.upload_dir / f"{feed_id}.part"
        try:
            received = await receive_upload(
                request.stream(), request.headers.get("content-type"), part_path, cfg.max_upload_bytes
            )
        except UploadTooLargeError as exc:
            raise ProblemError(413, "upload-too-large", "Upload too large", str(exc)) from None
        except MalformedUploadError as exc:
            status = 415 if "Content-Type" in str(exc) else 400
            raise ProblemError(status, "invalid-upload", "Invalid upload", str(exc)) from None
        except MultipartParseError as exc:
            raise ProblemError(
                400, "invalid-upload", "Invalid upload", f"malformed multipart body: {exc}"
            ) from None

        if received.path is None:
            raise ProblemError(
                422, "validation-error", "Request validation failed", "multipart field 'file' is required"
            )
        fmt = (received.fields.get("format") or "").lower() or _EXTENSIONS.get(
            Path(received.filename or "").suffix.lower(), ""
        )
        if fmt not in ("csv", "jsonl"):
            received.path.unlink(missing_ok=True)
            raise ProblemError(
                422,
                "unsupported-format",
                "Unsupported feed format",
                "format must be csv or jsonl (or inferable from a .csv/.jsonl/.ndjson filename)",
            )
        final_path = cfg.upload_dir / f"{feed_id}.{fmt}"
        received.path.rename(final_path)

        def insert() -> None:
            with request.app.state.pool.connection() as conn:
                conn.execute(
                    """
                    INSERT INTO feed_imports
                      (id, filename, format, status, byte_size, content_sha256, file_path, trace_context)
                    VALUES (%s, %s, %s, 'queued', %s, %s, %s, %s::jsonb)
                    """,
                    (
                        feed_id,
                        received.filename,
                        fmt,
                        received.size,
                        received.sha256,
                        str(final_path),
                        Json(inject_carrier()),
                    ),
                )

        try:
            await run_in_threadpool(insert)
        except BaseException:
            final_path.unlink(missing_ok=True)
            raise
        request.app.state.runner.submit(feed_id)
        log.info("feed %s accepted: %s (%d bytes, %s)", feed_id, received.filename, received.size, fmt)
        return JSONResponse(
            FeedAccepted(id=feed_id, status="queued").model_dump(mode="json"),
            status_code=202,
            headers={"Location": f"/feeds/{feed_id}"},
        )

    @app.get("/feeds/{feed_id}", response_model=FeedStatus, responses=_problem_responses(404))
    def get_feed(feed_id: UUID, request: Request) -> FeedStatus:
        with request.app.state.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                """
                SELECT id, status, rows_total, rows_ok, rows_rejected, error_counts, started_at, finished_at,
                       failure_reason
                FROM feed_imports WHERE id = %s
                """,
                (feed_id,),
            ).fetchone()
        if row is None:
            raise ProblemError(404, "not-found", "Feed not found", f"no feed {feed_id}")
        return FeedStatus.model_validate(row)

    @app.get("/feeds/{feed_id}/errors", response_model=FeedErrorsPage, responses=_problem_responses(400, 404))
    def get_feed_errors(
        feed_id: UUID,
        request: Request,
        cursor: Annotated[str | None, Query(description="next_cursor from the previous page")] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ) -> FeedErrorsPage:
        after = decode_cursor(cursor) if cursor else 0
        with request.app.state.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            exists = cur.execute("SELECT 1 FROM feed_imports WHERE id = %s", (feed_id,)).fetchone()
            if exists is None:
                raise ProblemError(404, "not-found", "Feed not found", f"no feed {feed_id}")
            rows = cur.execute(
                """
                SELECT id, row_number, code, field, message, raw FROM feed_row_errors
                WHERE feed_id = %s AND id > %s ORDER BY id LIMIT %s
                """,
                (feed_id, after, limit + 1),
            ).fetchall()
        more = len(rows) > limit
        page = rows[:limit]
        return FeedErrorsPage(
            items=[FeedRowError.model_validate(r) for r in page],
            next_cursor=encode_cursor(page[-1]["id"]) if more and page else None,
        )

    return app

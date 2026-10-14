"""Streaming multipart/form-data receiver: the file part goes straight to disk, chunk by chunk.

Starlette's ``UploadFile`` would spool the part to a temp file and then we would copy it again; parsing
the body stream ourselves writes each byte once and enforces the size limit while receiving.
"""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from python_multipart.multipart import MultipartParser, parse_options_header

MAX_FIELD_BYTES = 1024


class UploadTooLargeError(Exception):
    pass


class MalformedUploadError(Exception):
    pass


@dataclass
class ReceivedUpload:
    path: Path | None = None
    filename: str | None = None
    size: int = 0
    sha256: str = ""
    fields: dict[str, str] = field(default_factory=dict)


class _Collector:
    def __init__(self, dest: Path, max_bytes: int) -> None:
        self.dest = dest
        self.max_bytes = max_bytes
        self.result = ReceivedUpload()
        self._digest = hashlib.sha256()
        self._headers: dict[bytes, bytes] = {}
        self._hname = b""
        self._hval = b""
        self._name: str | None = None
        self._fh: IO[bytes] | None = None
        self._buf = bytearray()

    # python-multipart callbacks -------------------------------------------------
    def on_part_begin(self) -> None:
        self._headers = {}
        self._name = None
        self._buf = bytearray()

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._hname += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._hval += data[start:end]

    def on_header_end(self) -> None:
        self._headers[self._hname.strip().lower()] = self._hval.strip()
        self._hname = b""
        self._hval = b""

    def on_headers_finished(self) -> None:
        disposition = self._headers.get(b"content-disposition")
        if not disposition:
            raise MalformedUploadError("multipart part without Content-Disposition")
        _, params = parse_options_header(disposition)
        name = params.get(b"name")
        self._name = name.decode("utf-8", "replace") if name else None
        if self._name == "file":
            if self._fh is not None:
                raise MalformedUploadError("more than one file part")
            filename = params.get(b"filename")
            self.result.filename = filename.decode("utf-8", "replace") if filename else None
            self.result.path = self.dest
            self._fh = self.dest.open("wb")

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        chunk = data[start:end]
        if self._name == "file" and self._fh is not None:
            self.result.size += len(chunk)
            if self.result.size > self.max_bytes:
                raise UploadTooLargeError(f"upload exceeds {self.max_bytes} bytes")
            self._digest.update(chunk)
            self._fh.write(chunk)
        else:
            self._buf += chunk
            if len(self._buf) > MAX_FIELD_BYTES:
                raise MalformedUploadError(f"form field {self._name!r} is too large")

    def on_part_end(self) -> None:
        if self._name == "file" and self._fh is not None:
            self._fh.close()
        elif self._name:
            self.result.fields[self._name] = self._buf.decode("utf-8", "replace").strip()

    def callbacks(self) -> dict[str, Any]:
        return {
            "on_part_begin": self.on_part_begin,
            "on_header_field": self.on_header_field,
            "on_header_value": self.on_header_value,
            "on_header_end": self.on_header_end,
            "on_headers_finished": self.on_headers_finished,
            "on_part_data": self.on_part_data,
            "on_part_end": self.on_part_end,
        }

    def close(self) -> None:
        if self._fh is not None and not self._fh.closed:
            self._fh.close()
        self.result.sha256 = self._digest.hexdigest()


def boundary_from(content_type: str | None) -> bytes:
    if not content_type:
        raise MalformedUploadError("missing Content-Type")
    ctype, params = parse_options_header(content_type)
    if ctype != b"multipart/form-data":
        raise MalformedUploadError("Content-Type must be multipart/form-data")
    boundary = params.get(b"boundary")
    if not boundary:
        raise MalformedUploadError("multipart boundary missing")
    return boundary


def _remove(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


async def receive_upload(
    stream: AsyncIterator[bytes], content_type: str | None, dest: Path, max_bytes: int
) -> ReceivedUpload:
    """Parse the request body into ``dest``. On any error the partial file is removed."""
    collector = _Collector(dest, max_bytes)
    parser = MultipartParser(boundary_from(content_type), collector.callbacks())  # type: ignore[arg-type]
    try:
        async for chunk in stream:
            if chunk:
                parser.write(chunk)
        parser.finalize()
    except BaseException:
        collector.close()
        _remove(dest)
        raise
    collector.close()
    return collector.result

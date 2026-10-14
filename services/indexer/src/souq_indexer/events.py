"""catalog.* message model and Kafka header helpers (header names are a CLAUDE.md contract)."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

Headers = list[tuple[str, bytes]]

H_TRACEPARENT = "traceparent"
H_TRACESTATE = "tracestate"
H_EVENT_ID = "event_id"
H_SKU = "sku"
H_VERSION = "version"
H_ATTEMPT = "attempt"
H_NOT_BEFORE = "not_before"
H_ORIGINAL_TOPIC = "original_topic"
H_ERROR_CLASS = "error_class"
H_ERROR_MESSAGE = "error_message"

# Headers that describe one routing hop; they are replaced, never accumulated, when a message moves on.
ROUTING_HEADERS = frozenset({H_ATTEMPT, H_NOT_BEFORE, H_ORIGINAL_TOPIC, H_ERROR_CLASS, H_ERROR_MESSAGE})

MAX_ERROR_MESSAGE_BYTES = 2048


class ChangeEvent(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    event_id: str
    event_type: Literal["product.upserted", "product.deleted"]
    sku: str = Field(min_length=1, max_length=512)
    # OpenSearch external versions must be non-negative longs.
    version: int = Field(ge=0, le=2**63 - 1)
    outbox_created_at: datetime
    product: dict[str, Any] | None = None

    @field_validator("event_id", mode="before")
    @classmethod
    def _event_id_as_str(cls, value: Any) -> Any:
        return str(value) if isinstance(value, int) else value


class EventParseError(Exception):
    def __init__(self, error_class: str, message: str) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.message = message


def parse_event(value: bytes | None) -> ChangeEvent:
    if value is None:
        raise EventParseError("empty_value", "message has no value")
    try:
        data = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EventParseError("invalid_json", str(exc)) from exc
    try:
        event = ChangeEvent.model_validate(data)
    except ValidationError as exc:
        detail = "; ".join(
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise EventParseError("invalid_event", detail) from exc
    if event.event_type == "product.upserted" and not event.product:
        raise EventParseError("invalid_event", "product.upserted without a product payload")
    return event


def header_map(headers: Sequence[tuple[str, bytes | str | None]] | None) -> dict[str, str]:
    """Last value wins, decoded as UTF-8 (undecodable bytes are replaced rather than dropped)."""
    out: dict[str, str] = {}
    for key, value in headers or ():
        if value is None:
            continue
        out[key] = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
    return out


def normalise_headers(raw: Any) -> Headers:
    """confluent-kafka returns headers as a list of 2-sequences (or None); keep the bytes-valued ones."""
    out: Headers = []
    for pair in raw or ():
        key, value = pair[0], pair[1]
        if isinstance(value, bytes):
            out.append((str(key), value))
    return out


def header_int(headers: dict[str, str], name: str, default: int) -> int:
    raw = headers.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def encode_headers(pairs: Iterable[tuple[str, str | int]]) -> Headers:
    return [(k, str(v).encode("utf-8")) for k, v in pairs]


def truncate_utf8(text: str, limit: int = MAX_ERROR_MESSAGE_BYTES) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", "ignore") + "…"


def routed_headers(
    original: Sequence[tuple[str, bytes | None]] | None,
    *,
    attempt: int,
    not_before_ms: int | None,
    original_topic: str,
    error_class: str | None,
    error_message: str | None,
) -> Headers:
    """Original headers (trace context, event_id, sku, version) + a fresh set of routing headers."""
    kept: Headers = [(k, v) for k, v in (original or []) if k not in ROUTING_HEADERS and v is not None]
    extra: list[tuple[str, str | int]] = [(H_ATTEMPT, attempt), (H_ORIGINAL_TOPIC, original_topic)]
    if not_before_ms is not None:
        extra.append((H_NOT_BEFORE, not_before_ms))
    if error_class:
        extra.append((H_ERROR_CLASS, error_class))
    if error_message:
        extra.append((H_ERROR_MESSAGE, truncate_utf8(error_message)))
    return kept + encode_headers(extra)


@dataclass(slots=True)
class Envelope:
    """One consumed Kafka message plus what the pipeline learned about it."""

    topic: str
    partition: int
    offset: int
    key: bytes | None
    value: bytes | None
    headers: Headers
    received_ns: int
    attempt: int = 0
    original_topic: str = ""
    event: ChangeEvent | None = None
    parse_error: EventParseError | None = None
    meta: dict[str, str] = field(default_factory=dict)

    @property
    def sku(self) -> str:
        if self.event is not None:
            return self.event.sku
        if self.key is not None:
            return self.key.decode("utf-8", "replace")
        return self.meta.get(H_SKU, "")

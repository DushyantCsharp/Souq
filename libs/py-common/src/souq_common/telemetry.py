"""Telemetry facade shared by every Souq Python service (search, feeds, indexer, outbox-relay).

The functions listed under "souq_common.telemetry" in CLAUDE.md are a cross-team contract: their
signatures must not change. Everything here works when no SDK provider is installed
(``OTEL_SDK_DISABLED=true``, or ``init_telemetry`` never called) — spans become no-ops and instruments
discard data — so library code and unit tests can call it unconditionally.

Environment knobs:
  OTEL_SDK_DISABLED=true            install nothing (API stays no-op)
  OTEL_EXPORTER_OTLP_ENDPOINT       OTLP/HTTP base URL (e.g. http://localhost:4318). Exporters are only
                                    created when an endpoint is configured, so tests and CLIs run without
                                    a collector don't stall on shutdown retrying a dead endpoint.
  OTEL_TRACES_EXPORTER / OTEL_METRICS_EXPORTER   otlp (default) | console | none
  OTEL_METRIC_EXPORT_INTERVAL       ms between metric pushes (default 10000 — dashboards want < 60 s)
  OTEL_SERVICE_NAME                 overrides the name passed to init_telemetry()
  SOUQ_TELEMETRY_SALT               HMAC key for hash_query(); set the same value on every service so
                                    query hashes correlate across processes. Unset → random per process.
  SOUQ_TELEMETRY_RECORD_QUERY_TEXT  dev-only opt-in to put raw query text on spans; ignored when
                                    SOUQ_ENV is prod/production.
  SOUQ_ENV, SOUQ_VERSION            deployment.environment.name / service.version resource attributes
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib
import importlib.util
import json
import logging
import os
import secrets
import socket
import threading
import time
import unicodedata
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import metrics as otel_metrics
from opentelemetry import propagate, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    MetricReader,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace import Span as SdkSpan
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SpanExporter
from opentelemetry.trace import Link, Span, SpanKind, Status, StatusCode

__all__ = [
    "RedactingSpanProcessor",
    "SouqMetrics",
    "batch_span",
    "build_providers",
    "context_from_carrier",
    "continue_span",
    "extract_kafka_context",
    "hash_query",
    "init_telemetry",
    "inject_carrier",
    "inject_kafka_headers",
    "instrument_fastapi",
    "llm_span",
    "log_context",
    "metrics",
    "query_attributes",
    "record_llm_usage",
    "record_query_text",
    "shutdown_telemetry",
    "span_links",
    "stage",
]

_log = logging.getLogger("souq.telemetry")

INSTRUMENTATION_SCOPE = "souq"

# Histogram boundaries. The search SLOs are p95 <= 250 ms (standard) and <= 1200 ms (LLM path); with
# default exponential-ish buckets histogram_quantile interpolates across e.g. 250..500 and the SLO line is
# meaningless, so both SLO thresholds are exact bucket edges (burn-rate rules read le="250"/le="1200").
SEARCH_LATENCY_BUCKETS_MS: tuple[float, ...] = (
    5,
    10,
    25,
    50,
    75,
    100,
    150,
    200,
    250,
    300,
    400,
    500,
    750,
    1000,
    1200,
    1500,
    2000,
    3000,
    5000,
)
# Stage budgets in config/budgets.yaml range 5..800 ms; every budget value is a bucket edge.
STAGE_LATENCY_BUCKETS_MS: tuple[float, ...] = (
    1,
    2.5,
    5,
    10,
    15,
    20,
    30,
    45,
    50,
    60,
    80,
    90,
    120,
    150,
    200,
    250,
    400,
    600,
    800,
    1000,
    1200,
    2000,
)
# Index freshness target is p95 <= 30 s.
FRESHNESS_BUCKETS_S: tuple[float, ...] = (0.5, 1, 2, 5, 10, 15, 20, 30, 45, 60, 120, 300, 600, 1800)
# gen_ai.client.operation.duration (seconds); 0.8 = parse_llm_ms budget, 1.2 = LLM-path SLO.
LLM_DURATION_BUCKETS_S: tuple[float, ...] = (
    0.05,
    0.1,
    0.2,
    0.3,
    0.4,
    0.6,
    0.8,
    1.0,
    1.2,
    1.6,
    2.5,
    5,
    10,
    20,
)
# Semconv-recommended token buckets.
LLM_TOKEN_BUCKETS: tuple[float, ...] = (1, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144)
# http.server.request.duration from the FastAPI instrumentation (seconds); same SLO edges as above.
HTTP_DURATION_BUCKETS_S: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.075,
    0.1,
    0.15,
    0.2,
    0.25,
    0.3,
    0.5,
    0.75,
    1.0,
    1.2,
    1.5,
    2.5,
    5,
    10,
)

_TRUTHY = {"1", "true", "yes", "on"}
_PROD_ENVS = {"prod", "production"}


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


def _sdk_disabled() -> bool:
    return _env_flag("OTEL_SDK_DISABLED")


def _tracer() -> trace.Tracer:
    # Resolved per call (cheap) so a provider installed after import — by init_telemetry or a test
    # fixture — is always used.
    return trace.get_tracer(INSTRUMENTATION_SCOPE)


# --------------------------------------------------------------------------------------------- init

_init_lock = threading.Lock()
_initialised_as: str | None = None
_providers: list[TracerProvider | MeterProvider] = []


def init_telemetry(service_name: str) -> None:
    """Install the OTel SDK (resource, OTLP/HTTP exporters, metric reader, auto-instrumentation).

    Idempotent: the first call wins; later calls (any service name) are ignored, because OTel only
    allows one global provider per process. ``OTEL_SERVICE_NAME``, when set (infra/compose.apps.yaml sets it
    per service), overrides ``service_name`` so deployment config, not code, decides the name.
    """
    global _initialised_as
    service_name = os.environ.get("OTEL_SERVICE_NAME", "").strip() or service_name
    with _init_lock:
        if _initialised_as is not None:
            if service_name != _initialised_as:
                _log.warning(
                    "init_telemetry(%r) ignored: already initialised as %r", service_name, _initialised_as
                )
            return
        _initialised_as = service_name
        if _sdk_disabled():
            return
        _prefer_stable_http_semconv()
        tracer_provider, meter_provider = build_providers(service_name)
        trace.set_tracer_provider(tracer_provider)
        otel_metrics.set_meter_provider(meter_provider)
        _providers[:] = [tracer_provider, meter_provider]
        _auto_instrument()


def shutdown_telemetry(timeout_millis: int = 5000) -> None:
    """Flush and stop exporters. Providers also register an atexit hook; call this from SIGTERM handlers
    of long-running workers so the last batch is not lost when the process is killed after a grace period."""
    for provider in _providers:
        try:
            provider.force_flush(timeout_millis)
            provider.shutdown()
        except Exception:  # pragma: no cover - best effort on the way out
            _log.debug("telemetry shutdown failed", exc_info=True)
    _providers.clear()


def _otlp_endpoint_configured(signal: str) -> bool:
    return bool(
        os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        or os.environ.get(f"OTEL_EXPORTER_OTLP_{signal.upper()}_ENDPOINT")
    )


def _exporter_kind(signal: str) -> str:
    """'otlp' | 'console' | 'none' for ``signal`` in {'traces', 'metrics'}."""
    kind = os.environ.get(f"OTEL_{signal.upper()}_EXPORTER", "otlp").strip().lower() or "otlp"
    if kind == "otlp" and not _otlp_endpoint_configured(signal):
        return "none"
    return kind


def build_providers(
    service_name: str,
    *,
    span_exporter: SpanExporter | None = None,
    metric_readers: Sequence[MetricReader] | None = None,
) -> tuple[TracerProvider, MeterProvider]:
    """Construct (TracerProvider, MeterProvider) without installing them globally.

    ``init_telemetry`` uses this with env-driven exporters; tests and scripts can pass in-memory ones.
    """
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.namespace": "souq",
            "service.version": os.environ.get("SOUQ_VERSION", "dev"),
            "service.instance.id": f"{socket.gethostname()}-{os.getpid()}",
            "deployment.environment.name": os.environ.get("SOUQ_ENV", "dev"),
        }
    )

    tracer_provider = TracerProvider(resource=resource)
    exporter = span_exporter
    if exporter is None:
        kind = _exporter_kind("traces")
        if kind == "otlp":
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            exporter = OTLPSpanExporter()
        elif kind == "console":
            exporter = ConsoleSpanExporter()
    if exporter is not None:
        tracer_provider.add_span_processor(RedactingSpanProcessor(BatchSpanProcessor(exporter)))

    readers: list[MetricReader] = list(metric_readers or [])
    if metric_readers is None:
        kind = _exporter_kind("metrics")
        interval_ms = float(os.environ.get("OTEL_METRIC_EXPORT_INTERVAL", "10000"))
        if kind == "otlp":
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

            readers.append(PeriodicExportingMetricReader(OTLPMetricExporter(), interval_ms))
        elif kind == "console":
            readers.append(PeriodicExportingMetricReader(ConsoleMetricExporter(), interval_ms))

    views = [
        # Instruments created here carry bucket advice; this view covers the auto-instrumented HTTP
        # server histogram whose default buckets have no 1.2 s edge.
        View(
            instrument_name="http.server.request.duration",
            aggregation=ExplicitBucketHistogramAggregation(HTTP_DURATION_BUCKETS_S),
        ),
    ]
    meter_provider = MeterProvider(resource=resource, metric_readers=readers, views=views)
    return tracer_provider, meter_provider


# Attributes third-party instrumentations set that can carry user text. redis-py's instrumentation copies
# FT.SEARCH queries and *every returned document field* onto the span — for the semantic cache that is the
# cached parse of the user's query; URL query strings can carry ?q=.
_DROP_ATTR_PREFIXES = ("redis.search.xdoc_",)
_URL_ATTRS = ("url.full", "http.url", "http.target")


def _redact_attributes(attrs: Mapping[str, Any]) -> dict[str, Any] | None:
    """Sanitised copy of ``attrs``, or None when nothing needed changing (the common case: no copy)."""
    allow_text = record_query_text()
    changed = False
    out: dict[str, Any] = {}
    for key, value in attrs.items():
        if key.startswith(_DROP_ATTR_PREFIXES):
            changed = True
        elif allow_text:
            out[key] = value
        elif key == "redis.search.query":
            out["redis.search.query_hash"] = hash_query(str(value))
            changed = True
        elif key == "url.query" and value != "REDACTED":
            out[key] = "REDACTED"
            changed = True
        elif key in _URL_ATTRS and isinstance(value, str) and "?" in value:
            out[key] = value.split("?", 1)[0]
            changed = True
        else:
            out[key] = value
    return out if changed else None


class RedactingSpanProcessor(SpanProcessor):
    """Wraps the exporting processor and strips PII-prone attributes from finished spans before export.

    Sits in-process (not only in the collector) so nothing sensitive leaves the service even when it
    exports straight to a vendor endpoint.
    """

    def __init__(self, inner: SpanProcessor) -> None:
        self._inner = inner

    def on_start(self, span: SdkSpan, parent_context: otel_context.Context | None = None) -> None:
        self._inner.on_start(span, parent_context)

    def on_end(self, span: ReadableSpan) -> None:
        redacted = _redact_attributes(span.attributes or {})
        if redacted is not None:
            span = ReadableSpan(
                name=span.name,
                context=span.context,
                parent=span.parent,
                resource=span.resource,
                attributes=redacted,
                events=span.events,
                links=span.links,
                kind=span.kind,
                status=span.status,
                start_time=span.start_time,
                end_time=span.end_time,
                instrumentation_scope=span.instrumentation_scope,
            )
        self._inner.on_end(span)

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._inner.force_flush(timeout_millis)


def _prefer_stable_http_semconv() -> None:
    # Stable HTTP semconv (http.server.request.duration in seconds, url.path/url.query) matches what the
    # Node gateway emits, so one dashboard query works across runtimes. Must be set before the first
    # instrumentor runs; an explicit operator choice wins.
    os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http")


def _auto_instrument() -> None:
    """Instrument client libraries that are installed in this service; silently skip the rest."""
    targets: list[tuple[str, str, str, dict[str, Any]]] = [
        ("httpx", "opentelemetry.instrumentation.httpx", "HTTPXClientInstrumentor", {}),
        # Commenter off: it appends trace context into SQL text, defeating pg_stat_statements grouping.
        (
            "psycopg",
            "opentelemetry.instrumentation.psycopg",
            "PsycopgInstrumentor",
            {"enable_commenter": False},
        ),
        ("redis", "opentelemetry.instrumentation.redis", "RedisInstrumentor", {}),
    ]
    for library, module_name, class_name, kwargs in targets:
        if importlib.util.find_spec(library) is None:
            continue
        try:
            instrumentor = getattr(importlib.import_module(module_name), class_name)()
            if not instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.instrument(**kwargs)
        except Exception:
            _log.warning("auto-instrumentation of %s failed", library, exc_info=True)


# ------------------------------------------------------------------------------------------ metrics


@dataclass(frozen=True)
class SouqMetrics:
    """All Souq instruments. Names match CLAUDE.md "Telemetry names"; the two ratios
    (llm_path_ratio, semantic_cache_hit_ratio) are Prometheus recording rules over the counters here."""

    search_latency: Any  # histogram ms, attr path=standard|llm|cache
    stage_latency: Any  # histogram ms, attr stage
    degraded_total: Any  # counter, attr reason
    zero_result_total: Any  # counter
    llm_path_total: Any  # counter: searches whose parse took the LLM path (numerator of llm_path_ratio)
    search_total: Any  # counter: all searches (denominator of llm_path_ratio)
    semantic_cache_lookups_total: Any  # counter (denominator of semantic_cache_hit_ratio)
    semantic_cache_hits_total: Any  # counter (numerator of semantic_cache_hit_ratio)
    consumer_lag: Any  # gauge, attr partition (+ topic)
    bulk_rejections_total: Any  # counter
    index_freshness_seconds: Any  # histogram s: now - outbox_created_at when the doc becomes searchable
    dlq_depth: Any  # gauge
    indexer_batch_size: Any  # gauge (AIMD controller's current size)
    # Additions beyond the original contract (non-breaking):
    stage_budget_exceeded_total: Any  # counter, attr stage — recorded by stage()
    indexer_docs_total: Any  # counter, attr outcome=indexed|catalog.retry.30s|catalog.retry.5m|catalog.dlq
    llm_operation_duration: Any  # gen_ai.client.operation.duration (s) — recorded by llm_span()
    llm_token_usage: Any  # gen_ai.client.token.usage — recorded by record_llm_usage()
    llm_cost_usd_total: Any  # counter USD — recorded by record_llm_usage()


@lru_cache(maxsize=1)
def metrics() -> SouqMetrics:
    # The global meter is a proxy until a MeterProvider is installed, after which these instruments
    # forward to it — so calling metrics() at import time (before init_telemetry) is fine.
    m = otel_metrics.get_meter(INSTRUMENTATION_SCOPE)
    return SouqMetrics(
        search_latency=m.create_histogram(
            "search_latency",
            unit="ms",
            description="End-to-end search service latency by path",
            explicit_bucket_boundaries_advisory=SEARCH_LATENCY_BUCKETS_MS,
        ),
        stage_latency=m.create_histogram(
            "stage_latency",
            unit="ms",
            description="Search pipeline stage latency",
            explicit_bucket_boundaries_advisory=STAGE_LATENCY_BUCKETS_MS,
        ),
        degraded_total=m.create_counter("degraded_total", description="Degraded responses by reason"),
        zero_result_total=m.create_counter("zero_result_total", description="Searches returning no items"),
        llm_path_total=m.create_counter("llm_path_total", description="Searches parsed by the LLM"),
        search_total=m.create_counter("search_total", description="Searches served"),
        semantic_cache_lookups_total=m.create_counter(
            "semantic_cache_lookups_total", description="Semantic parse-cache lookups"
        ),
        semantic_cache_hits_total=m.create_counter(
            "semantic_cache_hits_total", description="Semantic parse-cache hits"
        ),
        consumer_lag=m.create_gauge(
            "consumer_lag", description="Kafka consumer lag (messages) per partition"
        ),
        bulk_rejections_total=m.create_counter(
            "bulk_rejections_total", description="OpenSearch bulk rejections (429 / es_rejected_execution)"
        ),
        index_freshness_seconds=m.create_histogram(
            "index_freshness_seconds",
            unit="s",
            description="Outbox write to searchable document delay",
            explicit_bucket_boundaries_advisory=FRESHNESS_BUCKETS_S,
        ),
        dlq_depth=m.create_gauge("dlq_depth", description="Messages waiting in catalog.dlq"),
        indexer_batch_size=m.create_gauge("indexer_batch_size", description="AIMD bulk batch size"),
        stage_budget_exceeded_total=m.create_counter(
            "stage_budget_exceeded_total", description="Stages that ran over their budget"
        ),
        indexer_docs_total=m.create_counter(
            "indexer_docs_total", description="Documents processed by outcome"
        ),
        llm_operation_duration=m.create_histogram(
            "gen_ai.client.operation.duration",
            unit="s",
            description="GenAI operation duration",
            explicit_bucket_boundaries_advisory=LLM_DURATION_BUCKETS_S,
        ),
        llm_token_usage=m.create_histogram(
            "gen_ai.client.token.usage",
            unit="{token}",
            description="GenAI tokens used per call",
            explicit_bucket_boundaries_advisory=LLM_TOKEN_BUCKETS,
        ),
        llm_cost_usd_total=m.create_counter("llm_cost_usd_total", unit="USD", description="LLM spend"),
    )


# ------------------------------------------------------------------------------- PII: query hashing

_fallback_salt: bytes | None = None
_salt_lock = threading.Lock()


def _salt() -> bytes:
    global _fallback_salt
    configured = os.environ.get("SOUQ_TELEMETRY_SALT")
    if configured:
        return configured.encode("utf-8")
    with _salt_lock:
        if _fallback_salt is None:
            # Unkeyed or fixed-key hashes of short queries are trivially reversible by dictionary attack;
            # a random per-process key keeps that property at the cost of cross-process correlation.
            _fallback_salt = secrets.token_bytes(32)
        return _fallback_salt


def hash_query(q: str) -> str:
    """Keyed hash (HMAC-SHA256, 64 bits hex) of the whitespace/case-canonical query, for telemetry."""
    canonical = " ".join(unicodedata.normalize("NFKC", q).split()).casefold()
    return hmac.new(_salt(), canonical.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def record_query_text() -> bool:
    """True only when the dev opt-in is set and we are not in production."""
    if os.environ.get("SOUQ_ENV", "dev").strip().lower() in _PROD_ENVS:
        return False
    return _env_flag("SOUQ_TELEMETRY_RECORD_QUERY_TEXT")


def query_attributes(q: str) -> dict[str, str | int]:
    """Span attributes describing a query without leaking it: hash + length (+ text if opted in)."""
    attrs: dict[str, str | int] = {"souq.query.hash": hash_query(q), "souq.query.length": len(q)}
    if record_query_text():
        attrs["souq.query.text"] = q
    return attrs


# Attribute leaf names that, by convention, carry user text. stage()/batch_span() attrs with these names
# are replaced by "<key>_hash" unless the dev opt-in is on — a backstop against accidental PII.
_TEXT_LEAVES = frozenset(
    {
        "q",
        "query",
        "query_text",
        "text",
        "original",
        "normalised",
        "rewritten",
        "residual",
        "prompt",
        "completion",
    }
)


def _attr_value(value: Any) -> Any:
    if isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        items = list(value)
        if all(isinstance(v, str) for v in items) or all(isinstance(v, bool | int | float) for v in items):
            return items
        return [str(v) for v in items]
    return str(value)


def _set_attrs(span: Span, prefix: str, attrs: Mapping[str, Any]) -> None:
    """Bare keys are namespaced under ``prefix`` (stage("parse", path="llm") → souq.parse.path);
    dotted keys are used verbatim."""
    if not span.is_recording():
        return
    allow_text = record_query_text()
    for key, value in attrs.items():
        if value is None:
            continue
        full_key = key if "." in key else f"{prefix}.{key}"
        if not allow_text and isinstance(value, str) and full_key.rsplit(".", 1)[-1] in _TEXT_LEAVES:
            span.set_attribute(f"{full_key}_hash", hash_query(value))
            continue
        span.set_attribute(full_key, _attr_value(value))


def _record_error(span: Span, exc: BaseException) -> str:
    """Mark the span failed without exporting the exception message: messages from pydantic, JSON
    decoding or LLM SDKs routinely embed the input (i.e. the user's query)."""
    error_type = type(exc).__qualname__
    if span.is_recording():
        span.set_attribute("error.type", error_type)
        event: dict[str, Any] = {"exception.type": f"{type(exc).__module__}.{error_type}"}
        if record_query_text():
            event["exception.message"] = str(exc)
        span.add_event("exception", event)
        span.set_status(Status(StatusCode.ERROR, error_type))
    return error_type


# ------------------------------------------------------------------------------------------- stages


@contextmanager
def stage(name: str, budget_ms: float | None = None, **attrs: Any) -> Iterator[Span]:
    """Span ``stage.<name>`` carrying its budget and whether it was exceeded; records ``stage_latency``.

    Bare keyword attrs are namespaced per stage: ``stage("parse", 5, path="rule")`` sets
    ``souq.parse.path``. A stage that raises TimeoutError, or is cancelled (asyncio.wait_for cancelling
    the coroutine that owns the stage) after its budget elapsed, counts as exceeded.
    """
    start = time.perf_counter()
    with _tracer().start_as_current_span(
        f"stage.{name}", record_exception=False, set_status_on_exception=False
    ) as span:
        span.set_attribute("souq.stage.name", name)
        if budget_ms is not None:
            span.set_attribute("souq.stage.budget_ms", float(budget_ms))
        _set_attrs(span, f"souq.{name}", attrs)
        outcome = "ok"
        try:
            yield span
        except BaseException as exc:
            if isinstance(exc, TimeoutError):
                outcome = "timeout"
            elif isinstance(exc, asyncio.CancelledError):
                outcome = "cancelled"
            else:
                outcome = "error"
            _record_error(span, exc)
            raise
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            exceeded = budget_ms is not None and (elapsed_ms > budget_ms or outcome == "timeout")
            span.set_attribute("souq.stage.duration_ms", round(elapsed_ms, 3))
            span.set_attribute("souq.stage.outcome", outcome)
            if budget_ms is not None:
                span.set_attribute("souq.stage.exceeded", exceeded)
            m = metrics()
            m.stage_latency.record(elapsed_ms, {"stage": name})
            if exceeded:
                m.stage_budget_exceeded_total.add(1, {"stage": name})


# ------------------------------------------------------------------------------ context propagation

Carrier = Mapping[str, Any]
KafkaHeaders = Sequence[tuple[str, bytes | str | None]]


def inject_carrier() -> dict[str, str]:
    """W3C carrier for the current context — stored in ``outbox.trace_context`` by writers."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


def context_from_carrier(carrier: Carrier | str | None) -> otel_context.Context:
    """Context from an ``outbox.trace_context`` value (jsonb dict, or its JSON text)."""
    if isinstance(carrier, str):
        try:
            carrier = json.loads(carrier) if carrier else None
        except ValueError:
            carrier = None
    if not isinstance(carrier, Mapping):
        return propagate.extract({})
    return propagate.extract({str(k).lower(): str(v) for k, v in carrier.items() if v is not None})


def inject_kafka_headers(headers: list[tuple[str, bytes]] | None = None) -> list[tuple[str, bytes]]:
    """Return ``headers`` plus traceparent/tracestate for the current context.

    Stale propagation headers (e.g. copied from the source message when re-publishing to a retry topic)
    are replaced, never duplicated; if there is no active context the originals are kept so the chain
    still links back to the producer.
    """
    carrier = inject_carrier()
    replaced = {k.lower() for k in carrier}
    if "traceparent" in replaced:
        replaced.add("tracestate")
    kept = [(k, v) for k, v in (headers or []) if k.lower() not in replaced]
    return kept + [(k, v.encode("utf-8")) for k, v in carrier.items()]


def _carrier_from_headers(headers: KafkaHeaders | Mapping[str, Any] | None) -> dict[str, str]:
    if not headers:
        return {}
    items: Iterable[tuple[str, Any]] = headers.items() if isinstance(headers, Mapping) else headers
    carrier: dict[str, str] = {}
    for key, value in items:
        if value is None:
            continue
        if isinstance(value, bytes | bytearray | memoryview):
            carrier[key.lower()] = bytes(value).decode("utf-8", "replace")
        else:
            carrier[key.lower()] = str(value)
    return carrier


def extract_kafka_context(headers: KafkaHeaders | Mapping[str, Any] | None) -> otel_context.Context:
    """Context from confluent-kafka ``msg.headers()`` (list of (str, bytes|None) or None)."""
    return propagate.extract(_carrier_from_headers(headers))


def _context_from_any(source: Carrier | KafkaHeaders | str | None) -> otel_context.Context:
    if source is None or isinstance(source, Mapping | str):
        return context_from_carrier(source)
    return extract_kafka_context(source)


def span_links(sources: Iterable[Carrier | KafkaHeaders | str | None], limit: int = 128) -> list[Link]:
    """Deduplicated span links to each message's producer context (outbox carriers or Kafka headers)."""
    links: list[Link] = []
    seen: set[tuple[int, int]] = set()
    for source in sources:
        span_context = trace.get_current_span(_context_from_any(source)).get_span_context()
        key = (span_context.trace_id, span_context.span_id)
        if not span_context.is_valid or key in seen:
            continue
        seen.add(key)
        links.append(Link(span_context))
        if len(links) >= limit:
            break
    return links


@contextmanager
def batch_span(
    name: str,
    sources: Iterable[Carrier | KafkaHeaders | str | None],
    *,
    kind: SpanKind = SpanKind.CONSUMER,
    **attrs: Any,
) -> Iterator[Span]:
    """Span for work over many messages (an outbox poll, a bulk request): one new span *linked* to every
    producer context instead of arbitrarily parenting under one of them."""
    materialised = list(sources)
    links = span_links(materialised)
    with _tracer().start_as_current_span(
        name, kind=kind, links=links, record_exception=False, set_status_on_exception=False
    ) as span:
        span.set_attribute("messaging.batch.message_count", len(materialised))
        _set_attrs(span, "souq", attrs)
        try:
            yield span
        except BaseException as exc:
            _record_error(span, exc)
            raise


@contextmanager
def continue_span(
    name: str,
    source: Carrier | KafkaHeaders | str | None,
    *,
    kind: SpanKind = SpanKind.CONSUMER,
    **attrs: Any,
) -> Iterator[Span]:
    """Span parented on a remote context (one outbox row / one Kafka message), so
    feeds → relay → indexer stays a single trace for that SKU change."""
    with _tracer().start_as_current_span(
        name,
        context=_context_from_any(source),
        kind=kind,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        _set_attrs(span, "souq", attrs)
        try:
            yield span
        except BaseException as exc:
            _record_error(span, exc)
            raise


def log_context() -> dict[str, str]:
    """trace_id/span_id of the current span for structured-log correlation ({} when none)."""
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return {}
    return {"trace_id": f"{span_context.trace_id:032x}", "span_id": f"{span_context.span_id:016x}"}


# ---------------------------------------------------------------------------------------- FastAPI


def _redact_server_span(span: Span, scope: dict[str, Any]) -> None:
    """Strip query strings from server spans; public endpoints may carry ?q=... ."""
    if not span.is_recording() or not scope.get("query_string"):
        return
    path = str(scope.get("path", ""))
    existing: Mapping[str, Any] = getattr(span, "attributes", None) or {}
    if "url.query" in existing:
        span.set_attribute("url.query", "REDACTED")
    if "http.target" in existing:
        span.set_attribute("http.target", path)
    for key in ("url.full", "http.url"):
        value = existing.get(key)
        if isinstance(value, str) and "?" in value:
            span.set_attribute(key, value.split("?", 1)[0])


def instrument_fastapi(app: Any) -> None:
    """Server spans + http.server.request.duration for a FastAPI app. Safe before or after
    init_telemetry (instruments resolve the global providers lazily); no-op when the SDK is disabled."""
    if _sdk_disabled() or getattr(app, "_is_instrumented_by_opentelemetry", False):
        return
    _prefer_stable_http_semconv()
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:  # pragma: no cover - dependency is declared, but stay non-fatal
        _log.warning("opentelemetry-instrumentation-fastapi not installed; %r not instrumented", app)
        return
    FastAPIInstrumentor.instrument_app(
        app,
        excluded_urls="healthz,readyz,metrics",
        server_request_hook=_redact_server_span,
        # Per-ASGI-message send/receive spans triple span volume and carry nothing actionable.
        exclude_spans=["receive", "send"],
    )


# ------------------------------------------------------------------------------------------- GenAI

_GEN_AI_PROVIDER = "anthropic"


@contextmanager
def llm_span(model: str, operation: str = "chat") -> Iterator[Span]:
    """GenAI client span (``"{operation} {model}"``) with gen_ai.* attributes.

    Never records prompt or completion text; failures carry only the exception type. Also records
    ``gen_ai.client.operation.duration``.
    """
    base = {
        "gen_ai.operation.name": operation,
        "gen_ai.provider.name": _GEN_AI_PROVIDER,
        "gen_ai.request.model": model,
    }
    start = time.perf_counter()
    error_type: str | None = None
    with _tracer().start_as_current_span(
        f"{operation} {model}",
        kind=SpanKind.CLIENT,
        attributes={
            **base,
            # Older semconv name, still what most GenAI dashboards group by.
            "gen_ai.system": _GEN_AI_PROVIDER,
            "souq.llm.mode": os.environ.get("SOUQ_LLM_MODE", "replay"),
        },
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except BaseException as exc:
            error_type = _record_error(span, exc)
            raise
        finally:
            metric_attrs: dict[str, str] = dict(base)
            if error_type is not None:
                metric_attrs["error.type"] = error_type
            metrics().llm_operation_duration.record(time.perf_counter() - start, metric_attrs)


def record_llm_usage(
    span: Span, input_tokens: int, output_tokens: int, cost_usd: float | None = None
) -> None:
    """Attach token usage (and optional cost) to an ``llm_span`` and record the usage metrics."""
    span.set_attribute("gen_ai.usage.input_tokens", int(input_tokens))
    span.set_attribute("gen_ai.usage.output_tokens", int(output_tokens))
    if cost_usd is not None:
        span.set_attribute("souq.llm.cost_usd", float(cost_usd))
    span_attrs: Mapping[str, Any] = getattr(span, "attributes", None) or {}
    attrs = {
        "gen_ai.operation.name": str(span_attrs.get("gen_ai.operation.name", "chat")),
        "gen_ai.provider.name": _GEN_AI_PROVIDER,
        "gen_ai.request.model": str(span_attrs.get("gen_ai.request.model", "unknown")),
    }
    m = metrics()
    m.llm_token_usage.record(int(input_tokens), {**attrs, "gen_ai.token.type": "input"})
    m.llm_token_usage.record(int(output_tokens), {**attrs, "gen_ai.token.type": "output"})
    if cost_usd is not None:
        m.llm_cost_usd_total.add(float(cost_usd), {"gen_ai.request.model": attrs["gen_ai.request.model"]})

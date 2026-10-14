"""Unit tests for souq_common.telemetry against in-memory exporters (no collector needed)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry import metrics as otel_metrics
from opentelemetry import trace
from opentelemetry.metrics import _internal as metrics_internal
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import HistogramDataPoint, InMemoryMetricReader, MetricsData
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode
from opentelemetry.util._once import Once

from souq_common import telemetry
from souq_common.telemetry import (
    batch_span,
    context_from_carrier,
    continue_span,
    extract_kafka_context,
    hash_query,
    inject_carrier,
    inject_kafka_headers,
    llm_span,
    metrics,
    query_attributes,
    record_llm_usage,
    stage,
)

RAW_QUERY = "red nike running shoes under 300 aed"


class Telemetry:
    def __init__(self, spans: InMemorySpanExporter, reader: InMemoryMetricReader) -> None:
        self.spans = spans
        self.reader = reader

    def finished(self) -> list[ReadableSpan]:
        return list(self.spans.get_finished_spans())

    def span(self, name: str) -> ReadableSpan:
        matches = [s for s in self.finished() if s.name == name]
        assert len(matches) == 1, f"expected one {name!r}, got {[s.name for s in self.finished()]}"
        return matches[0]

    def metric_points(self, name: str) -> list[Any]:
        data: MetricsData | None = self.reader.get_metrics_data()
        points: list[Any] = []
        for rm in data.resource_metrics if data else []:
            for sm in rm.scope_metrics:
                for metric in sm.metrics:
                    if metric.name == name:
                        points.extend(metric.data.data_points)
        return points


_GLOBALS = (
    (trace, "_TRACER_PROVIDER_SET_ONCE"),
    (trace, "_TRACER_PROVIDER"),
    (metrics_internal, "_METER_PROVIDER_SET_ONCE"),
    (metrics_internal, "_METER_PROVIDER"),
    (metrics_internal, "_PROXY_METER_PROVIDER"),
)


def _reset_globals() -> None:
    # OTel only lets a process set its providers once; tests need a known provider regardless of what
    # ran before, so reset the set-once guards (the same approach as opentelemetry-test-utils).
    trace._TRACER_PROVIDER_SET_ONCE = Once()  # type: ignore[attr-defined]
    trace._TRACER_PROVIDER = None  # type: ignore[attr-defined]
    metrics_internal._METER_PROVIDER_SET_ONCE = Once()
    metrics_internal._METER_PROVIDER = None
    # A fresh proxy, so meters handed out later are not bound to this module's (soon shut down) provider.
    metrics_internal._PROXY_METER_PROVIDER = metrics_internal._ProxyMeterProvider()


@pytest.fixture(scope="module")
def _providers() -> Iterator[Telemetry]:
    # SDK providers read OTEL_SDK_DISABLED at construction and become no-ops; other packages' conftests set it
    # for whole-repo pytest runs, so build ours with the SDK explicitly enabled.
    saved = [(module, name, getattr(module, name)) for module, name in _GLOBALS]
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("OTEL_SDK_DISABLED", "false")
        _reset_globals()
        exporter = InMemorySpanExporter()
        tracer_provider = TracerProvider()
        tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
        reader = InMemoryMetricReader()
        meter_provider = MeterProvider(metric_readers=[reader])
    trace.set_tracer_provider(tracer_provider)
    otel_metrics.set_meter_provider(meter_provider)
    telemetry.metrics.cache_clear()
    yield Telemetry(exporter, reader)
    meter_provider.shutdown()
    tracer_provider.shutdown()
    # Later test modules (whole-repo pytest run) must see the process exactly as it was — typically "no
    # SDK installed" — not a shut-down recording provider that still injects traceparent headers.
    for module, name, value in saved:
        setattr(module, name, value)
    telemetry.metrics.cache_clear()


@pytest.fixture
def otel(_providers: Telemetry, monkeypatch: pytest.MonkeyPatch) -> Telemetry:
    _providers.spans.clear()
    _providers.reader.get_metrics_data()  # drain; cumulative points remain but tests compare deltas
    for var in ("SOUQ_TELEMETRY_RECORD_QUERY_TEXT", "SOUQ_ENV"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    monkeypatch.setenv("SOUQ_TELEMETRY_SALT", "test-salt")
    return _providers


def _all_attribute_text(spans: list[ReadableSpan]) -> str:
    """Everything a span exports that could contain user text: name, attributes, event attributes."""
    parts: list[str] = []
    for s in spans:
        parts.append(s.name)
        parts.append(json.dumps(dict(s.attributes or {}), default=str, ensure_ascii=False))
        for event in s.events:
            parts.append(event.name)
            parts.append(json.dumps(dict(event.attributes or {}), default=str, ensure_ascii=False))
        if s.status.description:
            parts.append(s.status.description)
    return "\n".join(parts)


def _counter_value(otel: Telemetry, name: str, **attrs: str) -> float:
    return sum(
        p.value for p in otel.metric_points(name) if all(p.attributes.get(k) == v for k, v in attrs.items())
    )


# ------------------------------------------------------------------------------------------ stages


def test_stage_within_budget_sets_budget_and_not_exceeded(otel: Telemetry) -> None:
    with stage("normalise", budget_ms=50):
        pass
    span = otel.span("stage.normalise")
    attrs = span.attributes or {}
    assert attrs["souq.stage.name"] == "normalise"
    assert attrs["souq.stage.budget_ms"] == 50.0
    assert attrs["souq.stage.exceeded"] is False
    assert attrs["souq.stage.outcome"] == "ok"
    assert span.status.status_code is StatusCode.UNSET


def test_stage_over_budget_is_flagged_and_counted(otel: Telemetry) -> None:
    before = _counter_value(otel, "stage_budget_exceeded_total", stage="rerank")
    with stage("rerank", budget_ms=1):
        time.sleep(0.01)
    attrs = otel.span("stage.rerank").attributes or {}
    assert attrs["souq.stage.exceeded"] is True
    assert attrs["souq.stage.duration_ms"] > 1
    assert _counter_value(otel, "stage_budget_exceeded_total", stage="rerank") == before + 1
    points = [p for p in otel.metric_points("stage_latency") if p.attributes.get("stage") == "rerank"]
    assert points
    assert isinstance(points[0], HistogramDataPoint)
    assert points[0].count >= 1


def test_stage_without_budget_omits_budget_attributes(otel: Telemetry) -> None:
    with stage("fuse"):
        pass
    attrs = otel.span("stage.fuse").attributes or {}
    assert "souq.stage.budget_ms" not in attrs
    assert "souq.stage.exceeded" not in attrs


def test_stage_timeout_counts_as_exceeded_and_hides_message(otel: Telemetry) -> None:
    with pytest.raises(TimeoutError), stage("knn", budget_ms=60):
        raise TimeoutError(f"knn timed out for {RAW_QUERY}")
    span = otel.span("stage.knn")
    attrs = span.attributes or {}
    assert attrs["souq.stage.exceeded"] is True
    assert attrs["souq.stage.outcome"] == "timeout"
    assert span.status.status_code is StatusCode.ERROR
    assert RAW_QUERY not in _all_attribute_text([span])


async def test_stage_cancelled_by_wait_for_after_budget_is_exceeded(otel: Telemetry) -> None:
    async def rerank() -> None:
        with stage("rerank", budget_ms=5):
            await asyncio.sleep(1)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(rerank(), timeout=0.02)
    attrs = otel.span("stage.rerank").attributes or {}
    assert attrs["souq.stage.outcome"] == "cancelled"
    assert attrs["souq.stage.exceeded"] is True


def test_stage_attrs_are_namespaced_per_stage(otel: Telemetry) -> None:
    with stage("parse", budget_ms=5, path="rule", **{"souq.custom.flag": True}) as span:
        span.set_attribute("souq.parse.path", "llm")  # callers may refine after the fact
    attrs = otel.span("stage.parse").attributes or {}
    assert attrs["souq.parse.path"] == "llm"
    assert attrs["souq.custom.flag"] is True


def test_stage_parse_path_attribute_from_kwarg(otel: Telemetry) -> None:
    with stage("parse", budget_ms=5, path="cache_hit"):
        pass
    assert (otel.span("stage.parse").attributes or {})["souq.parse.path"] == "cache_hit"


def test_stages_nest_into_one_trace(otel: Telemetry) -> None:
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("POST /search"):
        for name in ("normalise", "parse", "embed", "bm25", "knn", "fuse", "rerank"):
            with stage(name, budget_ms=100):
                pass
    root = otel.span("POST /search")
    stages = [s for s in otel.finished() if s.name.startswith("stage.")]
    assert len(stages) == 7
    assert {s.context.trace_id for s in stages} == {root.context.trace_id}
    assert all(s.parent is not None and s.parent.span_id == root.context.span_id for s in stages)


# --------------------------------------------------------------------------------------- PII / hash


def test_no_raw_query_text_in_spans_by_default(otel: Telemetry) -> None:
    with stage("parse", budget_ms=5, q=RAW_QUERY, original=RAW_QUERY, **query_attributes(RAW_QUERY)):
        pass
    span = otel.span("stage.parse")
    assert RAW_QUERY not in _all_attribute_text([span])
    attrs = span.attributes or {}
    assert attrs["souq.parse.q_hash"] == hash_query(RAW_QUERY)
    assert attrs["souq.query.hash"] == hash_query(RAW_QUERY)
    assert attrs["souq.query.length"] == len(RAW_QUERY)


def test_query_text_opt_in_for_dev_only(otel: Telemetry, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOUQ_TELEMETRY_RECORD_QUERY_TEXT", "true")
    assert query_attributes(RAW_QUERY)["souq.query.text"] == RAW_QUERY
    monkeypatch.setenv("SOUQ_ENV", "prod")
    assert "souq.query.text" not in query_attributes(RAW_QUERY)


def test_hash_query_is_keyed_canonical_and_short(monkeypatch: pytest.MonkeyPatch) -> None:
    import hashlib

    monkeypatch.setenv("SOUQ_TELEMETRY_SALT", "salt-a")
    h = hash_query("Red  Nike Shoes")
    assert h == hash_query("  red nike shoes ")
    assert len(h) == 16
    assert int(h, 16) >= 0
    assert h != hashlib.sha256(b"red nike shoes").hexdigest()[:16]  # keyed, not a bare digest
    assert hash_query("حذاء رياضي") != h
    monkeypatch.setenv("SOUQ_TELEMETRY_SALT", "salt-b")
    assert hash_query("Red  Nike Shoes") != h


def test_hash_query_without_salt_is_stable_within_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOUQ_TELEMETRY_SALT", raising=False)
    assert hash_query("iphone 15") == hash_query("iphone 15")


# ------------------------------------------------------------------------------------- propagation


def test_kafka_header_round_trip_links_producer_and_consumer(otel: Telemetry) -> None:
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("relay publish") as producer:
        headers = inject_kafka_headers([("sku", b"B000123"), ("event_id", b"42")])
    keys = [k for k, _ in headers]
    assert keys[:2] == ["sku", "event_id"]
    assert keys.count("traceparent") == 1
    assert all(isinstance(v, bytes) for _, v in headers)

    ctx = extract_kafka_context(headers)
    with tracer.start_as_current_span("indexer consume", context=ctx):
        pass
    consumer = otel.span("indexer consume")
    assert consumer.context.trace_id == producer.get_span_context().trace_id
    assert consumer.parent is not None
    assert consumer.parent.span_id == producer.get_span_context().span_id


def test_kafka_headers_replace_stale_traceparent(otel: Telemetry) -> None:
    stale = [("traceparent", b"00-" + b"1" * 32 + b"-" + b"2" * 16 + b"-01"), ("tracestate", b"x=1")]
    with trace.get_tracer("test").start_as_current_span("retry republish") as span:
        headers = inject_kafka_headers([*stale, ("attempt", b"2")])
    traceparents = [v for k, v in headers if k == "traceparent"]
    assert len(traceparents) == 1
    assert f"{span.get_span_context().trace_id:032x}".encode() in traceparents[0]
    assert ("tracestate", b"x=1") not in headers
    assert ("attempt", b"2") in headers


def test_kafka_headers_kept_when_no_active_span(otel: Telemetry) -> None:
    original = [("traceparent", b"00-" + b"a" * 32 + b"-" + b"b" * 16 + b"-01")]
    assert inject_kafka_headers(list(original)) == original


def test_extract_kafka_context_tolerates_none_and_str_values(otel: Telemetry) -> None:
    assert not trace.get_current_span(extract_kafka_context(None)).get_span_context().is_valid
    tp = "00-" + "c" * 32 + "-" + "d" * 16 + "-01"
    ctx = extract_kafka_context([("TraceParent", tp), ("sku", None)])
    assert f"{trace.get_current_span(ctx).get_span_context().trace_id:032x}" == "c" * 32


def test_outbox_carrier_round_trip_via_json(otel: Telemetry) -> None:
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("feeds import") as writer:
        carrier = inject_carrier()
    assert "traceparent" in carrier
    for stored in (carrier, json.dumps(carrier)):  # jsonb dict or its text form
        with continue_span("relay row", stored, kind=SpanKind.PRODUCER):
            pass
    rows = [s for s in otel.finished() if s.name == "relay row"]
    assert len(rows) == 2
    assert all(s.context.trace_id == writer.get_span_context().trace_id for s in rows)
    assert all(s.kind is SpanKind.PRODUCER for s in rows)
    assert not trace.get_current_span(context_from_carrier(None)).get_span_context().is_valid
    assert not trace.get_current_span(context_from_carrier("not json")).get_span_context().is_valid


def test_batch_span_links_every_distinct_producer(otel: Telemetry) -> None:
    tracer = trace.get_tracer("test")
    carriers: list[Any] = []
    producer_ids = set()
    for i in range(3):
        with tracer.start_as_current_span(f"write {i}") as s:
            producer_ids.add(s.get_span_context().trace_id)
            carriers.append(inject_kafka_headers([("sku", f"S{i}".encode())]))
    carriers.append(carriers[0])  # duplicate message (redelivery) → one link
    carriers.append(None)  # message without context → skipped
    with batch_span("indexer bulk", carriers, **{"messaging.destination.name": "catalog.changes"}):
        pass
    span = otel.span("indexer bulk")
    assert {link.context.trace_id for link in span.links} == producer_ids
    assert len(span.links) == 3
    attrs = span.attributes or {}
    assert attrs["messaging.batch.message_count"] == 5
    assert attrs["messaging.destination.name"] == "catalog.changes"
    assert span.kind is SpanKind.CONSUMER
    assert span.context.trace_id not in producer_ids  # a new trace, linked rather than parented


# -------------------------------------------------------------------------------------------- GenAI


def test_llm_span_has_genai_attributes_and_usage(otel: Telemetry) -> None:
    with llm_span("claude-haiku-5-5") as span:
        record_llm_usage(span, input_tokens=412, output_tokens=57, cost_usd=0.00071)
    s = otel.span("chat claude-haiku-5-5")
    attrs = s.attributes or {}
    assert s.kind is SpanKind.CLIENT
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.provider.name"] == "anthropic"
    assert attrs["gen_ai.request.model"] == "claude-haiku-5-5"
    assert attrs["gen_ai.usage.input_tokens"] == 412
    assert attrs["gen_ai.usage.output_tokens"] == 57
    assert attrs["souq.llm.cost_usd"] == pytest.approx(0.00071)
    assert not any(
        k.startswith(("gen_ai.prompt", "gen_ai.completion", "gen_ai.input.messages")) for k in attrs
    )

    usage = otel.metric_points("gen_ai.client.token.usage")
    by_type = {p.attributes["gen_ai.token.type"]: p for p in usage}
    assert by_type["input"].sum >= 412
    assert by_type["output"].sum >= 57
    assert otel.metric_points("gen_ai.client.operation.duration")


def test_llm_span_error_records_type_not_message(otel: Telemetry) -> None:
    with pytest.raises(ValueError, match="shoes"), llm_span("claude-haiku-5-5", operation="chat"):
        raise ValueError(f"could not parse {RAW_QUERY}")
    s = otel.span("chat claude-haiku-5-5")
    assert s.status.status_code is StatusCode.ERROR
    assert (s.attributes or {})["error.type"] == "ValueError"
    assert RAW_QUERY not in _all_attribute_text([s])


# ------------------------------------------------------------------------------------------ metrics


def test_search_latency_buckets_resolve_slo_boundaries(otel: Telemetry) -> None:
    m = metrics()
    m.search_latency.record(180.0, {"path": "standard"})
    m.search_latency.record(900.0, {"path": "llm"})
    points = [p for p in otel.metric_points("search_latency") if isinstance(p, HistogramDataPoint)]
    assert points
    bounds = points[0].explicit_bounds
    assert 250 in bounds
    assert 1200 in bounds
    stage_bounds = [p for p in otel.metric_points("stage_latency") if isinstance(p, HistogramDataPoint)]
    with stage("bm25", budget_ms=90):
        pass
    stage_bounds = [p for p in otel.metric_points("stage_latency") if isinstance(p, HistogramDataPoint)]
    assert {5, 15, 30, 60, 90, 120, 800} <= set(stage_bounds[0].explicit_bounds)


def test_all_contract_metrics_exist_and_accept_records(otel: Telemetry) -> None:
    m = metrics()
    m.degraded_total.add(1, {"reason": "rerank"})
    m.zero_result_total.add(1)
    m.search_total.add(1, {"path": "llm"})
    m.llm_path_total.add(1)
    m.semantic_cache_lookups_total.add(1)
    m.semantic_cache_hits_total.add(1)
    m.consumer_lag.set(17, {"partition": "3", "topic": "catalog.changes"})
    m.bulk_rejections_total.add(2)
    m.index_freshness_seconds.record(4.2)
    m.dlq_depth.set(0)
    m.indexer_batch_size.set(1000)
    m.indexer_docs_total.add(10, {"outcome": "ok"})
    for name in (
        "degraded_total",
        "zero_result_total",
        "search_total",
        "llm_path_total",
        "semantic_cache_lookups_total",
        "semantic_cache_hits_total",
        "consumer_lag",
        "bulk_rejections_total",
        "index_freshness_seconds",
        "dlq_depth",
        "indexer_batch_size",
        "indexer_docs_total",
    ):
        assert otel.metric_points(name), name
    lag = otel.metric_points("consumer_lag")
    assert any(p.attributes.get("partition") == "3" and p.value == 17 for p in lag)


# ------------------------------------------------------------------------------------- FastAPI


def test_instrument_fastapi_creates_server_span_without_query_string(otel: Telemetry) -> None:
    fastapi = pytest.importorskip("fastapi")
    testclient = pytest.importorskip("fastapi.testclient")

    app = fastapi.FastAPI()

    @app.get("/v1/search")
    def search(q: str) -> dict[str, int]:
        with stage("normalise", budget_ms=5):
            return {"n": len(q)}

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    telemetry.instrument_fastapi(app)
    telemetry.instrument_fastapi(app)  # idempotent
    with testclient.TestClient(app) as client:
        assert client.get("/v1/search", params={"q": RAW_QUERY}).status_code == 200
        assert client.get("/healthz").status_code == 200
    spans = otel.finished()
    server = [s for s in spans if s.kind is SpanKind.SERVER]
    assert len(server) == 1, [s.name for s in spans]  # healthz excluded, no duplicate middleware
    assert "/v1/search" in server[0].name
    assert RAW_QUERY.split()[0] not in _all_attribute_text(spans)
    stage_span = otel.span("stage.normalise")
    assert stage_span.context.trace_id == server[0].context.trace_id


# ----------------------------------------------------------------------------------- init (process)


def _run_isolated(code: str, **env: str) -> subprocess.CompletedProcess[str]:
    # init_telemetry mutates process-global providers; run it in a fresh interpreter. os._exit skips
    # atexit flushing so nothing tries to reach the (absent) collector.
    full_env = {k: v for k, v in os.environ.items() if not k.startswith(("OTEL_", "SOUQ_"))}
    full_env.update(env)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        env=full_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_init_telemetry_installs_sdk_once_with_resource() -> None:
    result = _run_isolated(
        """
        import os
        from opentelemetry import trace, metrics
        from souq_common.telemetry import init_telemetry
        init_telemetry("search")
        tp = trace.get_tracer_provider()
        init_telemetry("search")
        init_telemetry("other")
        assert trace.get_tracer_provider() is tp, "second call must not replace the provider"
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.metrics import MeterProvider
        assert isinstance(tp, TracerProvider), type(tp)
        assert isinstance(metrics.get_meter_provider(), MeterProvider)
        attrs = tp.resource.attributes
        assert attrs["service.name"] == "search", attrs
        assert attrs["service.namespace"] == "souq"
        assert attrs["deployment.environment.name"] == "staging"
        assert os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] == "http"
        processors = tp._active_span_processor._span_processors
        assert len(processors) == 1, processors
        print("OK", flush=True)
        os._exit(0)
        """,
        OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:9",
        SOUQ_ENV="staging",
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_init_telemetry_respects_sdk_disabled() -> None:
    result = _run_isolated(
        """
        import os
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from souq_common.telemetry import init_telemetry, stage
        init_telemetry("search")
        assert not isinstance(trace.get_tracer_provider(), TracerProvider)
        with stage("normalise", budget_ms=5) as span:
            assert not span.is_recording()
        print("OK", flush=True)
        os._exit(0)
        """,
        OTEL_SDK_DISABLED="true",
        OTEL_EXPORTER_OTLP_ENDPOINT="http://127.0.0.1:9",
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_init_telemetry_without_endpoint_exports_nothing() -> None:
    result = _run_isolated(
        """
        import os
        from opentelemetry import trace
        from souq_common.telemetry import init_telemetry
        init_telemetry("indexer")
        tp = trace.get_tracer_provider()
        assert tp._active_span_processor._span_processors == (), "no exporter without an endpoint"
        print("OK", flush=True)
        os._exit(0)
        """
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


# ------------------------------------------------------------------------------------ redaction


def test_exporting_pipeline_redacts_third_party_pii_attributes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    exporter = InMemorySpanExporter()
    tracer_provider, meter_provider = telemetry.build_providers(
        "search", span_exporter=exporter, metric_readers=[InMemoryMetricReader()]
    )
    tracer = tracer_provider.get_tracer("redis-like")
    with tracer.start_as_current_span("redis.search") as span:
        span.set_attribute("redis.search.index", "semcache")
        span.set_attribute("redis.search.query", f"@q:{{{RAW_QUERY}}}")
        span.set_attribute("redis.search.xdoc_semcache:1.original", RAW_QUERY)
        span.set_attribute("db.statement", "FT.SEARCH ? ?")
    with tracer.start_as_current_span("GET") as span:
        span.set_attribute("url.full", f"http://gw/v1/search?q={RAW_QUERY}")
        span.set_attribute("url.query", f"q={RAW_QUERY}")
        span.set_attribute("url.path", "/v1/search")
    tracer_provider.force_flush()
    spans = list(exporter.get_finished_spans())
    assert len(spans) == 2
    assert RAW_QUERY not in _all_attribute_text(spans)
    redis_attrs = spans[0].attributes or {}
    assert redis_attrs["redis.search.index"] == "semcache"
    assert "redis.search.query_hash" in redis_attrs
    assert not any(k.startswith("redis.search.xdoc_") for k in redis_attrs)
    http_attrs = spans[1].attributes or {}
    assert http_attrs["url.full"] == "http://gw/v1/search"
    assert http_attrs["url.query"] == "REDACTED"
    assert http_attrs["url.path"] == "/v1/search"
    assert spans[1].resource.attributes["service.name"] == "search"
    tracer_provider.shutdown()
    meter_provider.shutdown()


def test_otel_service_name_env_overrides_code_default() -> None:
    result = _run_isolated(
        """
        import os
        from opentelemetry import trace
        from souq_common.telemetry import init_telemetry
        init_telemetry("souq-feeds")
        assert trace.get_tracer_provider().resource.attributes["service.name"] == "feeds"
        print("OK", flush=True)
        os._exit(0)
        """,
        OTEL_SERVICE_NAME="feeds",
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout

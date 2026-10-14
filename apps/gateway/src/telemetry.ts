// OpenTelemetry bootstrap for the gateway. Preloaded with `node --import ./dist/telemetry.js dist/server.js`
// so the ESM loader hook and instrumentations are in place before fastify/ioredis/graphql are imported —
// importing this from server.ts instead would be too late for ESM patching.
//
// One trace per search: HTTP server span (+ Fastify route span) → GraphQL execute → undici client span to
// services/search (W3C traceparent injected) → search's FastAPI span → stage.* spans.
//
// Env: OTEL_SDK_DISABLED, OTEL_EXPORTER_OTLP_ENDPOINT (no endpoint → spans are still created so context
// propagates to services/search, but nothing is exported), OTEL_METRIC_EXPORT_INTERVAL (default 10 s),
// SOUQ_ENV, SOUQ_VERSION, SOUQ_TELEMETRY_SALT, SOUQ_TELEMETRY_RECORD_QUERY_TEXT — same semantics as
// libs/py-common/src/souq_common/telemetry.py.
import { createHmac, randomBytes } from "node:crypto";
import { register } from "node:module";
import { hostname } from "node:os";

import { FastifyOtelInstrumentation } from "@fastify/otel";
import { type Attributes } from "@opentelemetry/api";
import { OTLPMetricExporter } from "@opentelemetry/exporter-metrics-otlp-proto";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import { GraphQLInstrumentation } from "@opentelemetry/instrumentation-graphql";
import { HttpInstrumentation } from "@opentelemetry/instrumentation-http";
import { IORedisInstrumentation } from "@opentelemetry/instrumentation-ioredis";
import { PinoInstrumentation } from "@opentelemetry/instrumentation-pino";
import { UndiciInstrumentation } from "@opentelemetry/instrumentation-undici";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { AggregationType, PeriodicExportingMetricReader, type ViewOptions } from "@opentelemetry/sdk-metrics";
import { NodeSDK, tracing } from "@opentelemetry/sdk-node";
import { createAddHookMessageChannel } from "import-in-the-middle";

const env = process.env;
const TRUTHY = new Set(["1", "true", "yes", "on"]);
const flag = (name: string): boolean => TRUTHY.has((env[name] ?? "").trim().toLowerCase());

/** Same buckets as the Python services: 250 ms and 1.2 s (the search SLOs) are exact edges. */
export const HTTP_DURATION_BUCKETS_S = [
  0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.2, 0.25, 0.3, 0.5, 0.75, 1.0, 1.2, 1.5, 2.5, 5, 10,
];

const UNTRACED_PATHS = new Set(["/healthz", "/readyz", "/metrics", "/favicon.ico"]);

// ---------------------------------------------------------------------------------------- PII helpers

let fallbackSalt: Buffer | undefined;

function salt(): Buffer | string {
  const configured = env.SOUQ_TELEMETRY_SALT;
  if (configured) return configured;
  // A fixed or empty key makes short-query hashes reversible by dictionary; random per process instead.
  fallbackSalt ??= randomBytes(32);
  return fallbackSalt;
}

/** Keyed hash (HMAC-SHA256, 64-bit hex) of the case/whitespace-canonical query; mirrors hash_query(). */
export function hashQuery(q: string): string {
  const canonical = q.normalize("NFKC").trim().split(/\s+/u).join(" ").toLowerCase();
  return createHmac("sha256", salt()).update(canonical, "utf8").digest("hex").slice(0, 16);
}

/** Dev-only opt-in to raw query text on spans; never honoured when SOUQ_ENV is prod/production. */
export function recordQueryText(): boolean {
  const deployment = (env.SOUQ_ENV ?? "dev").trim().toLowerCase();
  if (deployment === "prod" || deployment === "production") return false;
  return flag("SOUQ_TELEMETRY_RECORD_QUERY_TEXT");
}

/** Span attributes describing a query without leaking it (attach to the active span in route handlers). */
export function queryAttributes(q: string): Attributes {
  const attrs: Attributes = { "souq.query.hash": hashQuery(q), "souq.query.length": q.length };
  if (recordQueryText()) attrs["souq.query.text"] = q;
  return attrs;
}

function pathOnly(url: string): string {
  return url.split("?", 1)[0] ?? "";
}

const URL_ATTRS = ["url.full", "url.path", "http.url", "http.target"] as const;

/**
 * Strips query strings from every span just before it ends (GET /v1/search?q=… carries the user's search).
 * One generic pass instead of per-instrumentation hooks: http, @fastify/otel (url.path = raw request.url)
 * and undici each record the URL differently, and new instrumentations are covered automatically.
 * Same policy as RedactingSpanProcessor in souq_common.telemetry and the collector's transform/redact.
 */
export class RedactQueryStrings implements tracing.SpanProcessor {
  onStart(): void {}

  onEnding(span: tracing.Span): void {
    if (recordQueryText()) return;
    for (const key of URL_ATTRS) {
      const value = span.attributes[key];
      if (typeof value === "string" && value.includes("?")) span.setAttribute(key, pathOnly(value));
    }
    const query = span.attributes["url.query"];
    if (typeof query === "string" && query !== "") span.setAttribute("url.query", "REDACTED");
  }

  onEnd(): void {}

  forceFlush(): Promise<void> {
    return Promise.resolve();
  }

  shutdown(): Promise<void> {
    return Promise.resolve();
  }
}

// ------------------------------------------------------------------------------------------ bootstrap

function otlpConfigured(signal: "TRACES" | "METRICS"): boolean {
  const kind = (env[`OTEL_${signal}_EXPORTER`] ?? "otlp").trim().toLowerCase();
  return kind === "otlp" && Boolean(env.OTEL_EXPORTER_OTLP_ENDPOINT || env[`OTEL_EXPORTER_OTLP_${signal}_ENDPOINT`]);
}

let sdk: NodeSDK | undefined;

async function start(): Promise<void> {
  if (flag("OTEL_SDK_DISABLED")) return;
  // Stable HTTP semconv: http.server.request.duration in seconds, url.path/url.query/http.route — the same
  // names the Python services emit, so dashboards query one metric across runtimes.
  env.OTEL_SEMCONV_STABILITY_OPT_IN ??= "http";

  // Hook only the modules instrumentations ask for, instead of wrapping every ESM import.
  const { registerOptions, waitForAllMessagesAcknowledged } = createAddHookMessageChannel();
  register("import-in-the-middle/hook.mjs", import.meta.url, registerOptions);

  const views: ViewOptions[] = [
    {
      instrumentName: "http.server.request.duration",
      aggregation: { type: AggregationType.EXPLICIT_BUCKET_HISTOGRAM, options: { boundaries: HTTP_DURATION_BUCKETS_S } },
    },
    {
      instrumentName: "http.client.request.duration",
      aggregation: { type: AggregationType.EXPLICIT_BUCKET_HISTOGRAM, options: { boundaries: HTTP_DURATION_BUCKETS_S } },
    },
  ];

  sdk = new NodeSDK({
    resource: resourceFromAttributes({
      "service.name": env.OTEL_SERVICE_NAME ?? "gateway",
      "service.namespace": "souq",
      "service.version": env.SOUQ_VERSION ?? "dev",
      "service.instance.id": `${hostname()}-${process.pid}`,
      "deployment.environment.name": env.SOUQ_ENV ?? "dev",
    }),
    // Explicit processors: without them NodeSDK falls back to its own env-driven OTLP exporter even when no
    // endpoint is configured. The redactor runs in onEnding, before any processor exports the span.
    spanProcessors: [
      new RedactQueryStrings(),
      ...(otlpConfigured("TRACES") ? [new tracing.BatchSpanProcessor(new OTLPTraceExporter())] : []),
    ],
    metricReaders: otlpConfigured("METRICS")
      ? [
          new PeriodicExportingMetricReader({
            exporter: new OTLPMetricExporter(),
            exportIntervalMillis: Number(env.OTEL_METRIC_EXPORT_INTERVAL ?? 10_000),
          }),
        ]
      : [],
    views,
    // Logs stay on stdout (pino JSON); trace ids are injected into them by the pino instrumentation.
    logRecordProcessors: [],
    instrumentations: [
      new HttpInstrumentation({
        ignoreIncomingRequestHook: (req) => UNTRACED_PATHS.has(pathOnly(req.url ?? "")),
      }),
      // Outbound fetch() to services/search and services/feeds: client spans + traceparent injection.
      new UndiciInstrumentation(),
      new FastifyOtelInstrumentation({
        registerOnInitialization: true,
        ignorePaths: (route) => UNTRACED_PATHS.has(route.url),
      }),
      new GraphQLInstrumentation({
        // Literal argument values (search text) are replaced by "*" in graphql.source.
        allowValues: false,
        mergeItems: true,
        ignoreTrivialResolveSpans: true,
        depth: 3,
      }),
      new IORedisInstrumentation({
        // Default serializer can include keys/args; command name only (cache keys are derived from queries).
        dbStatementSerializer: (cmdName) => cmdName,
        requireParentSpan: true,
      }),
      new PinoInstrumentation(),
    ],
  });
  sdk.start();
  await waitForAllMessagesAcknowledged();
}

/** Flush and stop exporters; await from the gateway's SIGTERM path before process.exit(). */
export async function shutdownTelemetry(): Promise<void> {
  const current = sdk;
  sdk = undefined;
  if (!current) return;
  try {
    await current.shutdown();
  } catch (err) {
    console.warn(JSON.stringify({ level: "warn", msg: "telemetry shutdown failed", err: String(err) }));
  }
}

// Natural exit (event loop drained) — signal-driven exits must call shutdownTelemetry() explicitly.
process.once("beforeExit", () => {
  void shutdownTelemetry();
});

await start();

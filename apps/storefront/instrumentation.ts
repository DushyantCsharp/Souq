// Next.js instrumentation hook: Next calls register() once per server runtime at startup. Because the app
// lives in src/app, Next only discovers src/instrumentation.ts, which re-exports this module.
//
// Traces only. Once a provider is registered Next emits its own spans (request handling, rendering, route
// handlers); @vercel/otel adds fetch instrumentation that injects traceparent into calls to the gateway — the
// storefront's only upstream — so a search is one trace: storefront → gateway → services/search.
//
// Env (same semantics as the gateway and Python services): OTEL_SDK_DISABLED, OTEL_EXPORTER_OTLP_ENDPOINT
// (unset → nothing exported), OTEL_SERVICE_NAME, SOUQ_ENV, SOUQ_VERSION, SOUQ_TELEMETRY_RECORD_QUERY_TEXT,
// GATEWAY_URL (only this origin receives trace headers; third-party fetches never do).
import type { AttributeValue } from "@opentelemetry/api";
import type { ReadableSpan, SpanProcessor } from "@opentelemetry/sdk-trace-base";
import { registerOTel } from "@vercel/otel";

const TRUTHY = new Set(["1", "true", "yes", "on"]);
const flag = (name: string): boolean => TRUTHY.has((process.env[name] ?? "").trim().toLowerCase());

function recordQueryText(): boolean {
  const deployment = (process.env.SOUQ_ENV ?? "dev").trim().toLowerCase();
  if (deployment === "prod" || deployment === "production") return false;
  return flag("SOUQ_TELEMETRY_RECORD_QUERY_TEXT");
}

const URL_ATTRS = ["url.full", "url.path", "http.url", "http.target"] as const;

/**
 * Strips query strings from finished spans: Next records `http.target` = req.url, and the search page URL is
 * /<locale>/search?q=… (CLAUDE.md rule 6 — no query text in telemetry). @vercel/otel's composite processor
 * does not forward `onEnding`, so this scrubs in `onEnd`; it is listed before the exporting processor, and
 * the batch exporter serialises later from the same attributes object.
 */
class RedactQueryStrings implements SpanProcessor {
  onStart(): void {}

  onEnd(span: ReadableSpan): void {
    if (recordQueryText()) return;
    const attributes = span.attributes as Record<string, AttributeValue | undefined>;
    for (const key of URL_ATTRS) {
      const value = attributes[key];
      if (typeof value === "string" && value.includes("?")) attributes[key] = value.split("?", 1)[0];
    }
    const query = attributes["url.query"];
    if (typeof query === "string" && query !== "") attributes["url.query"] = "REDACTED";
  }

  forceFlush(): Promise<void> {
    return Promise.resolve();
  }

  shutdown(): Promise<void> {
    return Promise.resolve();
  }
}

function gatewayOrigin(): string {
  try {
    return new URL(process.env.GATEWAY_URL ?? "http://localhost:4000/graphql").origin;
  } catch {
    return "http://localhost:4000";
  }
}

export function register(): void {
  if (flag("OTEL_SDK_DISABLED")) return;
  registerOTel({
    serviceName: process.env.OTEL_SERVICE_NAME ?? "storefront",
    attributes: {
      "service.namespace": "souq",
      "service.version": process.env.SOUQ_VERSION ?? "dev",
      "deployment.environment.name": process.env.SOUQ_ENV ?? "dev",
    },
    spanProcessors: [new RedactQueryStrings(), "auto"],
    instrumentationConfig: {
      fetch: { propagateContextUrls: [gatewayOrigin()] },
    },
  });
}

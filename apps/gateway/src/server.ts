// Entry point. Telemetry is preloaded with `node --import ./dist/telemetry.js` (package.json start, Docker CMD)
// so ESM instrumentation is patched in before fastify/ioredis/graphql load.
import { trace } from "@opentelemetry/api";

import { buildApp } from "./app.js";
import { createRedis } from "./cache/redis.js";
import { createRedisResponseCache } from "./cache/response-cache.js";
import { loadBudgets } from "./config/budgets.js";
import { loadEnv } from "./config/env.js";
import { createFeedsClient } from "./feeds/client.js";
import { createSearchClient, createSearchProbe } from "./search/client.js";

interface TelemetryModule {
  shutdownTelemetry(): Promise<void>;
  queryAttributes(q: string): Record<string, string | number | boolean>;
}

/**
 * Same module instance as the `--import` preload (identical URL), so this adds no second SDK. Loaded by a
 * runtime specifier so the gateway still builds and runs while telemetry.ts is absent.
 */
async function loadTelemetry(): Promise<TelemetryModule | null> {
  const specifier = "./telemetry.js";
  try {
    return (await import(specifier)) as TelemetryModule;
  } catch {
    return null;
  }
}

const env = loadEnv();
const budgets = loadBudgets();
const telemetry = await loadTelemetry();

// Redis and the cache log through this before/independently of Fastify's logger; Errors are flattened
// because JSON.stringify drops their (non-enumerable) message.
const consoleLog = {
  warn: (obj: object, msg: string) => {
    const fields = Object.fromEntries(
      Object.entries(obj).map(([k, v]) => [k, v instanceof Error ? { name: v.name, message: v.message } : v]),
    );
    console.warn(JSON.stringify({ level: "warn", time: Date.now(), msg, ...fields }));
  },
};
const redis = env.redisUrl ? createRedis(env.redisUrl, budgets, consoleLog) : null;

const searchOptions = { baseUrl: env.searchUrl, timeoutMs: budgets.gateway.search_timeout_ms };
const app = await buildApp({
  env,
  budgets,
  searchClient: createSearchClient(searchOptions),
  feedsClient: createFeedsClient({ baseUrl: env.feedsUrl, timeoutMs: budgets.gateway.feeds_timeout_ms }),
  redis,
  cache: redis ? createRedisResponseCache(redis, consoleLog) : null,
  readiness: {
    required: { search: createSearchProbe(searchOptions) },
    informational: redis ? { redis: async () => (await redis.ping()) === "PONG" } : {},
  },
  // Keyed hash + length only, never the text (CLAUDE.md rule 6).
  ...(telemetry ? { annotate: (q: string) => trace.getActiveSpan()?.setAttributes(telemetry.queryAttributes(q)) } : {}),
});

if (redis) {
  // Startup must not wait for (or fail on) Redis: cache and rate limits fail open until it is reachable.
  redis.connect().catch((err: unknown) => app.log.warn({ err }, "redis connect failed; retrying in background"));
}

let closing = false;
async function shutdown(signal: string): Promise<void> {
  if (closing) return;
  closing = true;
  app.log.info({ signal }, "shutting down");
  try {
    await app.close();
  } finally {
    redis?.disconnect();
    // Flush the last spans/metrics; without this a SIGTERM drops the final export interval.
    await telemetry?.shutdownTelemetry();
  }
}
for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.once(signal, () => {
    void shutdown(signal).then(
      () => process.exit(0),
      () => process.exit(1),
    );
  });
}

await app.listen({ host: env.host, port: env.port });

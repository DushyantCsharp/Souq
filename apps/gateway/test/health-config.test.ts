import { afterEach, describe, expect, it } from "@jest/globals";
import type { FastifyInstance } from "fastify";

import { loadBudgets, parseTimeValueSeconds } from "../src/config/budgets.js";
import { loadEnv } from "../src/config/env.js";
import { testApp } from "./fixtures.js";

let app: FastifyInstance | undefined;
afterEach(async () => {
  await app?.close();
  app = undefined;
});

describe("health probes", () => {
  it("/healthz is unconditional", async () => {
    app = await testApp({ readiness: { required: { search: async () => false } } });
    const res = await app.inject("/healthz");
    expect(res.statusCode).toBe(200);
    expect(JSON.parse(res.body)).toEqual({ status: "ok" });
  });

  it("/readyz fails only on required checks", async () => {
    app = await testApp({
      readiness: { required: { search: async () => true }, informational: { redis: async () => false } },
    });
    const ok = await app.inject("/readyz");
    expect(ok.statusCode).toBe(200);
    expect(JSON.parse(ok.body)).toEqual({ status: "ready", checks: { search: "ok", redis: "failing" } });
    await app.close();

    app = await testApp({
      readiness: { required: { search: () => Promise.reject(new Error("down")) } },
    });
    const notReady = await app.inject("/readyz");
    expect(notReady.statusCode).toBe(503);
    expect(JSON.parse(notReady.body)).toEqual({ status: "not_ready", checks: { search: "failing" } });
  });
});

describe("configuration", () => {
  it("reads gateway budgets from config/budgets.yaml", () => {
    const budgets = loadBudgets();
    expect(budgets.gateway.search_timeout_ms).toBeGreaterThan(0);
    expect(budgets.gateway.feeds_timeout_ms).toBeGreaterThan(0);
    expect(budgets.pageTokenTtlSeconds).toBe(parseTimeValueSeconds(String(budgets.search.pit_keep_alive)));
  });

  it("parses OpenSearch time values", () => {
    expect(parseTimeValueSeconds("5m")).toBe(300);
    expect(parseTimeValueSeconds("30s")).toBe(30);
    expect(parseTimeValueSeconds("1h")).toBe(3600);
    expect(parseTimeValueSeconds("500ms")).toBe(0.5);
    expect(() => parseTimeValueSeconds("5 minutes")).toThrow();
  });

  it("requires a strong PAGE_TOKEN_SECRET in production", () => {
    expect(() => loadEnv({ NODE_ENV: "production" })).toThrow(/PAGE_TOKEN_SECRET/);
    expect(() => loadEnv({ PAGE_TOKEN_SECRET: "short" })).toThrow(/at least 32 bytes/);
    const dev = loadEnv({});
    expect(dev.pageTokenSecretEphemeral).toBe(true);
    expect(dev.apiKeys).toEqual([]);
    expect(loadEnv({ GATEWAY_API_KEYS: " a, ,b " }).apiKeys).toEqual(["a", "b"]);
    expect(loadEnv({ REDIS_URL: "" }).redisUrl).toBeNull();
    expect(dev.trustProxy).toBe(false);
    expect(loadEnv({ TRUST_PROXY: "true" }).trustProxy).toBe(true);
    expect(loadEnv({ TRUST_PROXY: "10.0.0.0/8, 127.0.0.1" }).trustProxy).toEqual(["10.0.0.0/8", "127.0.0.1"]);
  });
});

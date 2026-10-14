import { afterEach, describe, expect, it } from "@jest/globals";
import type { FastifyInstance } from "fastify";

import { testApp, testEnv } from "./fixtures.js";

let app: FastifyInstance | undefined;
afterEach(async () => {
  await app?.close();
  app = undefined;
});

const limited = () => testEnv({ rateLimit: { max: 2, timeWindow: "1 minute" } });

describe("rate limiting", () => {
  it("answers the request over the limit with 429 problem+json and Retry-After", async () => {
    app = await testApp({ env: limited() });

    const ok1 = await app.inject("/v1/search?q=a");
    const ok2 = await app.inject("/v1/search?q=b");
    const limitedRes = await app.inject("/v1/search?q=c");

    expect([ok1.statusCode, ok2.statusCode]).toEqual([200, 200]);
    expect(ok1.headers["x-ratelimit-limit"]).toBe("2");
    expect(ok2.headers["x-ratelimit-remaining"]).toBe("0");
    expect(limitedRes.statusCode).toBe(429);
    expect(limitedRes.headers["content-type"]).toBe("application/problem+json");
    const retryAfter = Number(limitedRes.headers["retry-after"]);
    expect(retryAfter).toBeGreaterThan(0);
    expect(retryAfter).toBeLessThanOrEqual(60);
    expect(JSON.parse(limitedRes.body)).toEqual(
      expect.objectContaining({ type: "https://souq.dev/problems/rate-limited", status: 429, instance: "/v1/search" }),
    );
  });

  it("applies to GraphQL as well", async () => {
    app = await testApp({ env: limited() });
    const query = { query: "{ __typename }" };
    await app.inject({ method: "POST", url: "/graphql", payload: query });
    await app.inject({ method: "POST", url: "/graphql", payload: query });
    const res = await app.inject({ method: "POST", url: "/graphql", payload: query });
    expect(res.statusCode).toBe(429);
  });

  it("never limits health probes", async () => {
    app = await testApp({ env: limited() });
    for (let i = 0; i < 5; i++) {
      expect((await app.inject("/healthz")).statusCode).toBe(200);
    }
  });

  it("gives a valid API key its own bucket but ignores invalid keys", async () => {
    app = await testApp({ env: limited() });
    await app.inject("/v1/search?q=a");
    await app.inject("/v1/search?q=a");

    // Same IP, junk key: still the exhausted IP bucket.
    const junk = await app.inject({ url: "/v1/search?q=a", headers: { "x-api-key": "made-up" } });
    expect(junk.statusCode).toBe(429);

    const keyed = await app.inject({ url: "/v1/search?q=a", headers: { "x-api-key": "key-one" } });
    expect(keyed.statusCode).toBe(200);
  });
});

describe("client IP for rate limiting", () => {
  it("ignores X-Forwarded-For unless TRUST_PROXY says otherwise", async () => {
    app = await testApp({ env: limited() });
    await app.inject({ url: "/v1/search?q=a", headers: { "x-forwarded-for": "1.1.1.1" } });
    await app.inject({ url: "/v1/search?q=a", headers: { "x-forwarded-for": "2.2.2.2" } });
    const spoofed = await app.inject({ url: "/v1/search?q=a", headers: { "x-forwarded-for": "3.3.3.3" } });
    expect(spoofed.statusCode).toBe(429);
  });
});

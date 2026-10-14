import { afterEach, describe, expect, it } from "@jest/globals";
import type { FastifyInstance } from "fastify";

import { createMemoryResponseCache } from "../src/cache/response-cache.js";
import { UpstreamError } from "../src/errors/upstream.js";
import { decodePageToken, encodePageToken } from "../src/pagination/page-token.js";
import { CURSOR, fakeSearchClient, searchResponse, TEST_SECRET, testApp } from "./fixtures.js";

let app: FastifyInstance | undefined;
afterEach(async () => {
  await app?.close();
  app = undefined;
});

function json(body: string): Record<string, unknown> {
  return JSON.parse(body) as Record<string, unknown>;
}

describe("GET /v1/search", () => {
  it("maps the internal response to the public contract", async () => {
    const client = fakeSearchClient();
    app = await testApp({ searchClient: client });

    const res = await app.inject({ method: "GET", url: "/v1/search?q=red%20nike%20shoes&lang=en" });

    expect(res.statusCode).toBe(200);
    expect(res.headers["content-type"]).toMatch(/^application\/json/);
    expect(res.headers["x-souq-cache"]).toBe("bypass");
    expect(res.headers["x-request-id"]).toBeTruthy();
    const body = json(res.body);
    expect(body.items).toEqual([
      expect.objectContaining({ sku: "B000123", rank_source: "rerank", description: null, size: null }),
      expect.objectContaining({ sku: "B000456", rank_source: "fused" }),
    ]);
    expect(body.applied_filters).toEqual({ brands: ["Nike"], colours: ["red"], price_max: 300 });
    expect(body.parsed_query).toEqual(expect.objectContaining({ parser: "rule", rewritten: "nike shoes" }));
    expect(body.degraded).toEqual([]);
    expect(body.total_estimate).toBe(2);
    expect(body).not.toHaveProperty("timings_ms");
    // The next cursor is never exposed raw: only as a signed token bound to this query.
    expect(typeof body.next_page_token).toBe("string");
    expect(decodePageToken(body.next_page_token as string, { q: "red nike shoes", lang: "en" }, { secret: TEST_SECRET })).toEqual(
      CURSOR,
    );
    expect(client.calls[0]?.request).toEqual({ q: "red nike shoes", lang: "en", limit: 24 });
  });

  it("passes items through in upstream order", async () => {
    const response = searchResponse();
    response.items.reverse();
    app = await testApp({ searchClient: fakeSearchClient(async () => response) });
    const body = json((await app.inject("/v1/search?q=shoes")).body);
    expect((body.items as { sku: string }[]).map((i) => i.sku)).toEqual(["B000456", "B000123"]);
  });

  it("parses deepObject filters, repeated keys and limit", async () => {
    const client = fakeSearchClient();
    app = await testApp({ searchClient: client });

    const res = await app.inject({
      method: "GET",
      url:
        "/v1/search?q=shoes&limit=10&filters[brands]=nike&filters[brands]=adidas&filters[price_max]=300" +
        "&filters[price_min]=50&filters[currency]=aed&filters[colours]=Red&filters[category]=Fashion%20%3E%20Shoes",
    });

    expect(res.statusCode).toBe(200);
    expect(client.calls[0]?.request).toEqual({
      q: "shoes",
      lang: "auto",
      limit: 10,
      filters: {
        brands: ["nike", "adidas"],
        price_max: 300,
        price_min: 50,
        currency: "AED",
        colours: ["red"],
        category: "Fashion > Shoes",
      },
    });
  });

  it.each([
    ["missing q", "/v1/search", "q"],
    ["blank q", "/v1/search?q=%20%20", "q"],
    ["bad lang", "/v1/search?q=x&lang=fr", "lang"],
    ["limit too big", "/v1/search?q=x&limit=51", "limit"],
    ["limit not int", "/v1/search?q=x&limit=2.5", "limit"],
    ["unknown filter", "/v1/search?q=x&filters[weight]=3", "filters[weight]"],
    ["negative price", "/v1/search?q=x&filters[price_max]=-1", "filters[price_max]"],
    ["bad currency", "/v1/search?q=x&filters[currency]=dirham", "filters[currency]"],
    ["inverted range", "/v1/search?q=x&filters[price_min]=10&filters[price_max]=5", "filters[price_min]"],
    ["bare filters", "/v1/search?q=x&filters=nike", "filters"],
  ])("rejects %s with 400 validation-error", async (_name, url, param) => {
    const client = fakeSearchClient();
    app = await testApp({ searchClient: client });

    const res = await app.inject(url);

    expect(res.statusCode).toBe(400);
    expect(res.headers["content-type"]).toBe("application/problem+json");
    const body = json(res.body);
    expect(body).toEqual(
      expect.objectContaining({
        type: "https://souq.dev/problems/validation-error",
        status: 400,
        instance: "/v1/search",
      }),
    );
    expect((body.errors as { name: string }[]).map((e) => e.name)).toContain(param);
    expect(client.calls).toHaveLength(0);
  });

  it("follows a page token: cursor goes upstream, query must match", async () => {
    const client = fakeSearchClient();
    app = await testApp({ searchClient: client });

    const first = json((await app.inject("/v1/search?q=shoes&filters[brands]=nike")).body);
    const token = encodeURIComponent(first.next_page_token as string);

    const second = await app.inject(`/v1/search?q=shoes&filters[brands]=nike&page_token=${token}`);
    expect(second.statusCode).toBe(200);
    expect(client.calls[1]?.request.cursor).toEqual(CURSOR);

    const mismatch = await app.inject(`/v1/search?q=boots&filters[brands]=nike&page_token=${token}`);
    expect(mismatch.statusCode).toBe(400);
    expect(json(mismatch.body)).toEqual(
      expect.objectContaining({
        type: "https://souq.dev/problems/invalid-page-token",
        detail: "The page token was issued for a different query.",
      }),
    );
    expect(client.calls).toHaveLength(2);
  });

  it("rejects tampered and expired tokens", async () => {
    app = await testApp();
    const query = { q: "shoes", lang: "auto" };
    const valid = encodePageToken(CURSOR, query, { secret: TEST_SECRET, ttlSeconds: 300 });
    const [body, sig] = valid.split(".");
    const forged = `${Buffer.from(JSON.stringify({ c: { ...CURSOR, pit_id: "other" }, qh: "x", exp: 9e9 })).toString("base64url")}.${sig}`;
    const expired = encodePageToken(CURSOR, query, { secret: TEST_SECRET, ttlSeconds: 1, now: Date.now() - 10_000 });

    for (const token of [forged, `${body}.AAAA`, "not-a-token", expired]) {
      const res = await app.inject(`/v1/search?q=shoes&page_token=${encodeURIComponent(token)}`);
      expect(res.statusCode).toBe(400);
      expect(json(res.body).type).toBe("https://souq.dev/problems/invalid-page-token");
    }
  });

  it("reports a signed token whose snapshot is gone upstream as invalid-page-token", async () => {
    const client = fakeSearchClient(async (request) => {
      if (request.cursor) throw new UpstreamError("search", "rejected", "invalid cursor", { status: 400 });
      return searchResponse();
    });
    app = await testApp({ searchClient: client });
    const first = json((await app.inject("/v1/search?q=shoes")).body);
    const res = await app.inject(`/v1/search?q=shoes&page_token=${encodeURIComponent(first.next_page_token as string)}`);
    expect(res.statusCode).toBe(400);
    expect(json(res.body)).toEqual(
      expect.objectContaining({ type: "https://souq.dev/problems/invalid-page-token", detail: expect.stringMatching(/restart/) }),
    );
  });

  it("serves repeats from the response cache, with canonical keys", async () => {
    const client = fakeSearchClient();
    const cache = createMemoryResponseCache();
    app = await testApp({ searchClient: client, cache });

    const a = await app.inject("/v1/search?q=red%20shoes&filters[brands]=b&filters[brands]=a");
    const b = await app.inject("/v1/search?q=%20red%20%20shoes&filters[brands]=a&filters[brands]=b&lang=AUTO");

    expect(a.headers["x-souq-cache"]).toBe("miss");
    expect(b.headers["x-souq-cache"]).toBe("hit");
    expect(json(b.body)).toEqual(json(a.body));
    expect(client.calls).toHaveLength(1);
  });

  it("does not cache degraded responses when no degraded TTL is configured", async () => {
    const client = fakeSearchClient(async () => searchResponse({ degraded: ["rerank"] }));
    const cache = createMemoryResponseCache();
    app = await testApp({ searchClient: client, cache });

    await app.inject("/v1/search?q=shoes");
    const second = await app.inject("/v1/search?q=shoes");

    expect(json(second.body).degraded).toEqual(["rerank"]);
    expect(second.headers["x-souq-cache"]).toBe("miss");
    expect(cache.size()).toBe(0);
    expect(client.calls).toHaveLength(2);
  });

  it("returns null next_page_token on the last page", async () => {
    app = await testApp({ searchClient: fakeSearchClient(async () => searchResponse({ next_cursor: null })) });
    expect(json((await app.inject("/v1/search?q=shoes")).body).next_page_token).toBeNull();
  });

  it.each([
    [new UpstreamError("search", "timeout", "search service exceeded its latency budget"), 504, "upstream-timeout"],
    [new UpstreamError("search", "unavailable", "search service is unreachable"), 503, "search-unavailable"],
    [new UpstreamError("search", "bad_response", "bad body"), 502, "bad-gateway"],
    [new Error("boom"), 500, "internal"],
  ])("maps upstream failure %# to problem+json", async (error, status, slug) => {
    app = await testApp({ searchClient: fakeSearchClient(() => Promise.reject(error)) });
    const res = await app.inject("/v1/search?q=shoes");
    expect(res.statusCode).toBe(status);
    expect(res.headers["content-type"]).toBe("application/problem+json");
    const body = json(res.body);
    expect(body.type).toBe(`https://souq.dev/problems/${slug}`);
    expect(body.request_id).toBe(res.headers["x-request-id"]);
    // Internal error text never leaks.
    if (status === 500) expect(JSON.stringify(body)).not.toContain("boom");
  });

  it("never echoes query text in problem bodies", async () => {
    app = await testApp({ searchClient: fakeSearchClient(() => Promise.reject(new Error("x"))) });
    const res = await app.inject("/v1/search?q=secret%20query");
    expect(res.body).not.toContain("secret");
  });

  it("returns problem+json for unknown routes", async () => {
    app = await testApp();
    const res = await app.inject("/v1/nope");
    expect(res.statusCode).toBe(404);
    expect(json(res.body).type).toBe("https://souq.dev/problems/not-found");
  });
});

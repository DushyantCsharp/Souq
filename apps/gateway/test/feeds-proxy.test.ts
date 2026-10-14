import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import { Readable } from "node:stream";

import { afterAll, afterEach, beforeAll, describe, expect, it } from "@jest/globals";
import type { FastifyInstance } from "fastify";

import { createFeedsClient } from "../src/feeds/client.js";
import { testApp, testEnv, TEST_BUDGETS } from "./fixtures.js";

// A stand-in for services/feeds that records exactly what the gateway forwarded.
interface Received {
  method: string;
  url: string;
  headers: IncomingMessage["headers"];
  body: Buffer;
}
let received: Received[] = [];
let respond: (req: Received, res: ServerResponse) => void = (_req, res) => {
  res.writeHead(500).end();
};
let feeds: Server;
let feedsUrl: string;

beforeAll(async () => {
  feeds = createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => chunks.push(c));
    req.on("end", () => {
      const r = { method: req.method ?? "", url: req.url ?? "", headers: req.headers, body: Buffer.concat(chunks) };
      received.push(r);
      respond(r, res);
    });
  });
  await new Promise<void>((resolve) => feeds.listen(0, "127.0.0.1", resolve));
  feedsUrl = `http://127.0.0.1:${(feeds.address() as AddressInfo).port}`;
});
afterAll(async () => {
  await new Promise<void>((resolve) => feeds.close(() => resolve()));
});

let app: FastifyInstance | undefined;
afterEach(async () => {
  await app?.close();
  app = undefined;
  received = [];
});

function sendJson(res: ServerResponse, status: number, body: unknown): void {
  res.writeHead(status, { "content-type": "application/json" }).end(JSON.stringify(body));
}

async function gateway(maxUploadBytes = 1024 * 1024): Promise<FastifyInstance> {
  return testApp({
    env: testEnv({ feedsMaxUploadBytes: maxUploadBytes }),
    feedsClient: createFeedsClient({ baseUrl: feedsUrl, timeoutMs: TEST_BUDGETS.gateway.feeds_timeout_ms }),
  });
}

const BOUNDARY = "----souqtestboundary";
function multipart(csv: string): Buffer {
  return Buffer.from(
    `--${BOUNDARY}\r\nContent-Disposition: form-data; name="format"\r\n\r\ncsv\r\n` +
      `--${BOUNDARY}\r\nContent-Disposition: form-data; name="file"; filename="feed.csv"\r\n` +
      `Content-Type: text/csv\r\n\r\n${csv}\r\n--${BOUNDARY}--\r\n`,
  );
}
const MULTIPART = `multipart/form-data; boundary=${BOUNDARY}`;
const AUTH = { "x-api-key": "key-two" };

describe("feeds proxy", () => {
  it("streams the multipart body through byte-for-byte and returns 202 + Location", async () => {
    respond = (_req, res) => sendJson(res, 202, { id: "f-123", status: "queued" });
    app = await gateway();
    const payload = multipart("sku,title_en,brand,category_path,price,currency\nB1,Shoe,Nike,A > B,10,AED\n");

    const res = await app.inject({
      method: "POST",
      url: "/v1/feeds",
      headers: { ...AUTH, "content-type": MULTIPART },
      payload,
    });

    expect(res.statusCode).toBe(202);
    expect(res.headers.location).toBe("/v1/feeds/f-123");
    expect(JSON.parse(res.body)).toEqual({ id: "f-123", status: "queued" });
    expect(received).toHaveLength(1);
    expect(received[0]?.method).toBe("POST");
    expect(received[0]?.url).toBe("/feeds");
    expect(received[0]?.headers["content-type"]).toBe(MULTIPART);
    expect(received[0]?.headers["x-request-id"]).toBe(res.headers["x-request-id"]);
    expect(received[0]?.headers["x-api-key"]).toBeUndefined();
    expect(received[0]?.body.equals(payload)).toBe(true);
  });

  it("streams chunked uploads without a content-length", async () => {
    respond = (_req, res) => sendJson(res, 202, { id: 42, status: "queued" });
    app = await gateway();
    const payload = multipart("x".repeat(64 * 1024));
    const chunks = [payload.subarray(0, 1000), payload.subarray(1000, 40_000), payload.subarray(40_000)];

    const res = await app.inject({
      method: "POST",
      url: "/v1/feeds",
      headers: { ...AUTH, "content-type": MULTIPART },
      payload: Readable.from(chunks),
    });

    expect(res.statusCode).toBe(202);
    expect(JSON.parse(res.body)).toEqual({ id: "42", status: "queued" });
    expect(received[0]?.body.equals(payload)).toBe(true);
  });

  it("requires an API key before reading the body", async () => {
    app = await gateway();
    for (const headers of [{}, { "x-api-key": "wrong" }]) {
      const res = await app.inject({
        method: "POST",
        url: "/v1/feeds",
        headers: { ...headers, "content-type": MULTIPART },
        payload: multipart("a"),
      });
      expect(res.statusCode).toBe(401);
      expect(JSON.parse(res.body).type).toBe("https://souq.dev/problems/unauthorized");
    }
    expect((await app.inject({ url: "/v1/feeds/abc" })).statusCode).toBe(401);
    expect(received).toHaveLength(0);
  });

  it("rejects non-multipart bodies with 415", async () => {
    app = await gateway();
    const res = await app.inject({ method: "POST", url: "/v1/feeds", headers: AUTH, payload: { file: "x" } });
    expect(res.statusCode).toBe(415);
    expect(JSON.parse(res.body).type).toBe("https://souq.dev/problems/unsupported-media-type");
    expect(received).toHaveLength(0);
  });

  it("rejects oversized uploads by declared length and while streaming", async () => {
    respond = (_req, res) => sendJson(res, 202, { id: "never", status: "queued" });
    app = await gateway(1024);
    const big = multipart("y".repeat(4096));

    const declared = await app.inject({
      method: "POST",
      url: "/v1/feeds",
      headers: { ...AUTH, "content-type": MULTIPART },
      payload: big,
    });
    expect(declared.statusCode).toBe(413);

    const streamed = await app.inject({
      method: "POST",
      url: "/v1/feeds",
      headers: { ...AUTH, "content-type": MULTIPART },
      payload: Readable.from([big.subarray(0, 800), big.subarray(800)]),
    });
    expect(streamed.statusCode).toBe(413);
    expect(JSON.parse(streamed.body).type).toBe("https://souq.dev/problems/payload-too-large");
  });

  it("maps feeds-service validation errors to 400 problem+json", async () => {
    respond = (_req, res) =>
      sendJson(res, 422, { detail: [{ loc: ["body", "format"], msg: "Field required", type: "missing" }] });
    app = await gateway();
    const res = await app.inject({
      method: "POST",
      url: "/v1/feeds",
      headers: { ...AUTH, "content-type": MULTIPART },
      payload: multipart("a"),
    });
    expect(res.statusCode).toBe(400);
    expect(JSON.parse(res.body).detail).toContain("body.format: Field required");
  });

  it("returns feed status with row-level error counts", async () => {
    respond = (req, res) =>
      sendJson(res, 200, {
        id: "f-1",
        status: "completed",
        rows_total: 10,
        rows_ok: 7,
        rows_rejected: 3,
        error_counts: { missing_field: 2, invalid_price: 1 },
        started_at: "2026-10-13T10:00:00Z",
        finished_at: "2026-10-13T10:00:05Z",
        failure_reason: null,
        internal_only: req.url,
      });
    app = await gateway();
    const res = await app.inject({ url: "/v1/feeds/f-1", headers: AUTH });
    expect(res.statusCode).toBe(200);
    expect(JSON.parse(res.body)).toEqual({
      id: "f-1",
      status: "completed",
      rows_total: 10,
      rows_ok: 7,
      rows_rejected: 3,
      error_counts: { missing_field: 2, invalid_price: 1 },
      started_at: "2026-10-13T10:00:00Z",
      finished_at: "2026-10-13T10:00:05Z",
      failure_reason: null,
    });
  });

  it("paginates row errors, passing the token through as the feeds cursor", async () => {
    respond = (_req, res) =>
      sendJson(res, 200, {
        items: [{ row_number: 4, code: "invalid_price", field: "price", message: "not a number", raw: "B1,,x" }],
        next_cursor: "c-2",
      });
    app = await gateway();
    const res = await app.inject({ url: "/v1/feeds/f-1/errors?limit=1&page_token=c-1", headers: AUTH });
    expect(res.statusCode).toBe(200);
    expect(received[0]?.url).toBe("/feeds/f-1/errors?limit=1&cursor=c-1");
    expect(JSON.parse(res.body)).toEqual({
      items: [{ row_number: 4, code: "invalid_price", field: "price", message: "not a number", raw: "B1,,x" }],
      next_page_token: "c-2",
    });
  });

  it("maps unknown feeds to 404 and validates ids and limits before forwarding", async () => {
    respond = (_req, res) => sendJson(res, 404, { detail: "feed not found" });
    app = await gateway();
    expect((await app.inject({ url: "/v1/feeds/missing", headers: AUTH })).statusCode).toBe(404);
    expect((await app.inject({ url: "/v1/feeds/..%2Fadmin", headers: AUTH })).statusCode).toBe(400);
    expect((await app.inject({ url: "/v1/feeds/f-1/errors?limit=0", headers: AUTH })).statusCode).toBe(400);
    expect(received).toHaveLength(1);
  });

  it("reports an unreachable feeds service as 503", async () => {
    app = await testApp({
      feedsClient: createFeedsClient({ baseUrl: "http://127.0.0.1:1", timeoutMs: 500 }),
    });
    const res = await app.inject({ url: "/v1/feeds/f-1", headers: AUTH });
    expect(res.statusCode).toBe(503);
    expect(JSON.parse(res.body).type).toBe("https://souq.dev/problems/feeds-unavailable");
  });
});

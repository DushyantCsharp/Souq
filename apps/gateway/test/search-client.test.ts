import { createServer, type Server, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";

import { afterAll, beforeAll, describe, expect, it } from "@jest/globals";

import { UpstreamError } from "../src/errors/upstream.js";
import { createSearchClient, createSearchProbe } from "../src/search/client.js";
import { searchResponse } from "./fixtures.js";

// Stub services/search: behaviour picked per request by the `q` field.
let server: Server;
let baseUrl: string;
const seen: { url: string; body: unknown; requestId: string | undefined }[] = [];

beforeAll(async () => {
  server = createServer((req, res: ServerResponse) => {
    let raw = "";
    req.on("data", (c: Buffer) => (raw += c.toString()));
    req.on("end", () => {
      if (req.url === "/readyz") return void res.writeHead(200).end("{}");
      const body = JSON.parse(raw) as { q: string };
      seen.push({ url: req.url ?? "", body, requestId: req.headers["x-request-id"] as string | undefined });
      const json = (status: number, payload: unknown) =>
        res.writeHead(status, { "content-type": "application/json" }).end(JSON.stringify(payload));
      switch (body.q) {
        case "slow":
          setTimeout(() => json(200, searchResponse()), 1500);
          return;
        case "down":
          return json(503, { detail: "opensearch unavailable" });
        case "invalid":
          return json(422, { detail: [{ loc: ["body", "limit"], msg: "too big", type: "x" }] });
        case "garbage":
          return void res.writeHead(200, { "content-type": "application/json" }).end("{not json");
        case "wrong-shape":
          return json(200, { hello: "world" });
        default:
          return json(200, searchResponse());
      }
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  baseUrl = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});
afterAll(async () => {
  await new Promise<void>((resolve) => server.close(() => resolve()));
});

async function failure(q: string, timeoutMs = 100): Promise<UpstreamError> {
  const client = createSearchClient({ baseUrl, timeoutMs });
  const err = await client.search({ q, lang: "auto", limit: 24 }).then(
    () => undefined,
    (e: unknown) => e,
  );
  expect(err).toBeInstanceOf(UpstreamError);
  return err as UpstreamError;
}

describe("search client (openapi-fetch over the generated spec)", () => {
  it("POSTs /search with the request body and request id", async () => {
    const client = createSearchClient({ baseUrl, timeoutMs: 1000 });
    const res = await client.search({ q: "shoes", lang: "en", limit: 5 }, { requestId: "req-1" });
    expect(res.items).toHaveLength(2);
    expect(seen.at(-1)).toEqual({ url: "/search", body: { q: "shoes", lang: "en", limit: 5 }, requestId: "req-1" });
  });

  it("enforces the latency budget with AbortSignal.timeout", async () => {
    const started = Date.now();
    const err = await failure("slow", 50);
    expect(err.failure).toBe("timeout");
    // Well under the stub's 1.5 s delay, with headroom for a loaded CI runner.
    expect(Date.now() - started).toBeLessThan(750);
  });

  it.each([
    ["down", "unavailable", 503],
    ["invalid", "rejected", 422],
    ["garbage", "bad_response", undefined],
    ["wrong-shape", "bad_response", 200],
  ])("classifies %s as %s", async (q, kind, status) => {
    const err = await failure(q, 1000);
    expect(err.failure).toBe(kind);
    expect(err.status).toBe(status);
  });

  it("carries FastAPI validation detail into the message", async () => {
    expect((await failure("invalid", 1000)).message).toContain("body.limit: too big");
  });

  it("treats connection refused as unavailable", async () => {
    const client = createSearchClient({ baseUrl: "http://127.0.0.1:1", timeoutMs: 500 });
    await expect(client.search({ q: "x" })).rejects.toMatchObject({ failure: "unavailable" });
  });

  it("probes readiness", async () => {
    expect(await createSearchProbe({ baseUrl, timeoutMs: 500 })()).toBe(true);
    expect(await createSearchProbe({ baseUrl: "http://127.0.0.1:1", timeoutMs: 500 })()).toBe(false);
  });
});

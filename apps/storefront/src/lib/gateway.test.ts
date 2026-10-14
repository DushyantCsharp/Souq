/** @jest-environment node */
import { describe, expect, it, jest } from "@jest/globals";
import { GatewayError, gatewayQuery, isInvalidPageTokenError } from "./gateway";
import { TypedDocumentString } from "./operations";


const doc = new TypedDocumentString<{ ping: string }, { x: number }>("query Ping($x: Int) { ping }");

function respond(status: number, body: unknown): typeof fetch {
  return jest.fn<typeof fetch>(async () =>
    new Response(typeof body === "string" ? body : JSON.stringify(body), {
      status,
      headers: { "content-type": "application/json" },
    }),
  );
}

const opts = (fetchImpl: typeof fetch) => ({ url: "http://gw.test/graphql", fetchImpl, timeoutMs: 1000 });

describe("gatewayQuery", () => {
  it("posts the document text and variables and returns data", async () => {
    const fetchImpl = respond(200, { data: { ping: "pong" } });
    await expect(gatewayQuery(doc, { x: 1 }, opts(fetchImpl))).resolves.toEqual({ ping: "pong" });
    const [url, init] = (fetchImpl as jest.Mock<typeof fetch>).mock.calls[0] ?? [];
    expect(url).toBe("http://gw.test/graphql");
    expect(JSON.parse(String(init?.body))).toEqual({ query: "query Ping($x: Int) { ping }", variables: { x: 1 } });
  });

  it("maps invalid page tokens to their own error kind", async () => {
    const fetchImpl = respond(200, {
      data: null,
      errors: [{ message: "bad cursor", extensions: { type: "https://souq.dev/problems/invalid-page-token" } }],
    });
    await expect(gatewayQuery(doc, { x: 1 }, opts(fetchImpl))).rejects.toMatchObject({ kind: "invalid_page_token" });
  });

  it("reports network failures as unavailable", async () => {
    const fetchImpl = jest.fn<typeof fetch>(async () => {
      throw new TypeError("fetch failed");
    });
    const err = await gatewayQuery(doc, { x: 1 }, opts(fetchImpl)).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(GatewayError);
    expect(err).toMatchObject({ kind: "unavailable" });
  });

  it("rejects non-JSON and data-less responses", async () => {
    await expect(gatewayQuery(doc, { x: 1 }, opts(respond(502, "<html>")))).rejects.toMatchObject({
      kind: "bad_response",
      status: 502,
    });
    await expect(
      gatewayQuery(doc, { x: 1 }, opts(respond(500, { errors: [{ message: "boom" }] }))),
    ).rejects.toMatchObject({ kind: "unavailable" });
    await expect(
      gatewayQuery(
        doc,
        { x: 1 },
        opts(respond(200, { data: null, errors: [{ message: "t/o", extensions: { code: "UPSTREAM_TIMEOUT" } }] })),
      ),
    ).rejects.toMatchObject({ kind: "unavailable" });
    await expect(
      gatewayQuery(doc, { x: 1 }, opts(respond(200, { errors: [{ message: "q too long", extensions: { code: "BAD_USER_INPUT" } }] }))),
    ).rejects.toMatchObject({ kind: "bad_response" });
  });
});

describe("isInvalidPageTokenError", () => {
  it("recognises the code, the problem type and the message", () => {
    expect(isInvalidPageTokenError({ message: "x", extensions: { code: "INVALID_PAGE_TOKEN" } })).toBe(true);
    expect(isInvalidPageTokenError({ message: "Invalid page token" })).toBe(true);
    expect(isInvalidPageTokenError({ message: "other" })).toBe(false);
  });
});

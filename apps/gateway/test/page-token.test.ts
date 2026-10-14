// Public page tokens (CLAUDE.md "Public page token"):
//   base64url(JSON{c, qh: sha256(q|lang|filters canonical), exp}) + "." + base64url(HMAC-SHA256(secret, body))
// The format is checked independently with node:crypto, then every way a client can damage a token must be
// rejected with InvalidPageTokenError (the REST/GraphQL layers map it to 400 invalid-page-token).
import { createHash, createHmac } from "node:crypto";

import { describe, expect, it } from "@jest/globals";

import {
  decodePageToken,
  encodePageToken,
  InvalidPageTokenError,
  type InvalidPageTokenReason,
  type PageTokenQuery,
} from "../src/pagination/page-token.js";
import type { Cursor } from "../src/search/types.js";

const SECRET = "page-token-test-secret-0123456789abcdef";
const NOW = Date.UTC(2026, 9, 13, 10, 0, 0);
const TTL_S = 300;

const CURSOR: Cursor = {
  query_key: "qk-7f3a",
  pit_id: "46ToAwMDaWR5BXV1aWQyKwZub2RlXzMAAAAAAAAAACoBYwADaWR4BXV1aWQxAgZub2RlXzEAAAAAAAAAAAEBYQ==",
  window_pos: 24,
  search_after: [12.5, "B000123"],
};

const QUERY: PageTokenQuery = {
  q: "black running shoes",
  lang: "en",
  filters: { brands: ["nike", "adidas"], price_max: 300 },
};

const issue = (cursor: Cursor = CURSOR, query: PageTokenQuery = QUERY, secret = SECRET): string =>
  encodePageToken(cursor, query, { secret, ttlSeconds: TTL_S, now: NOW });

function rejection(fn: () => unknown): InvalidPageTokenError {
  try {
    fn();
  } catch (err) {
    expect(err).toBeInstanceOf(InvalidPageTokenError);
    return err as InvalidPageTokenError;
  }
  throw new Error("expected InvalidPageTokenError, nothing was thrown");
}

function expectReason(fn: () => unknown, reason: InvalidPageTokenReason): void {
  expect(rejection(fn).reason).toBe(reason);
}

const B64URL = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

/** Replace one character with a different valid base64url character, so the token still *parses*. */
function flipChar(s: string, index: number): string {
  const c = s[index];
  if (c === undefined) throw new Error(`index ${index} out of range`);
  const replacement = B64URL[(B64URL.indexOf(c) + 1) % B64URL.length] ?? "A";
  return s.slice(0, index) + replacement + s.slice(index + 1);
}

function split(token: string): [string, string] {
  const [body, sig] = token.split(".");
  if (body === undefined || sig === undefined) throw new Error(`not a page token: ${token}`);
  return [body, sig];
}

describe("page token format", () => {
  it("is base64url(JSON body) + '.' + base64url(HMAC-SHA256(secret, body))", () => {
    const token = issue();
    expect(token).toMatch(/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/);
    const [body, sig] = split(token);
    expect(sig).toBe(createHmac("sha256", SECRET).update(body).digest("base64url"));

    const payload = JSON.parse(Buffer.from(body, "base64url").toString("utf8")) as Record<string, unknown>;
    expect(Object.keys(payload).sort()).toEqual(["c", "exp", "qh"]);
    expect(payload.c).toEqual(CURSOR);
    expect(payload.exp).toBe(Math.floor(NOW / 1000) + TTL_S);
    expect(payload.qh).toMatch(/^[0-9a-f]{64}$/);
  });

  it("binds qh to sha256(q|lang|canonical filters)", () => {
    const [body] = split(issue());
    const { qh } = JSON.parse(Buffer.from(body, "base64url").toString("utf8")) as { qh: string };
    // Canonical filters: sorted keys, sorted de-duplicated arrays.
    const canonical = JSON.stringify({ brands: ["adidas", "nike"], price_max: 300 });
    const expected = createHash("sha256").update(`black running shoes|en|${canonical}`).digest("hex");
    expect(qh).toBe(expected);
  });

  it("does not leak the query text into the token", () => {
    const [body] = split(issue());
    expect(Buffer.from(body, "base64url").toString("utf8")).not.toContain("running");
  });
});

describe("decodePageToken accepts", () => {
  it("round-trips the cursor untouched", () => {
    expect(decodePageToken(issue(), QUERY, { secret: SECRET, now: NOW })).toEqual(CURSOR);
  });

  it("is still valid one second before expiry", () => {
    const now = NOW + (TTL_S - 1) * 1000;
    expect(decodePageToken(issue(), QUERY, { secret: SECRET, now })).toEqual(CURSOR);
  });

  it("the same query with cosmetic differences (filter order, duplicates, whitespace)", () => {
    const token = issue();
    const reordered: PageTokenQuery = {
      q: "  black   running shoes ",
      lang: "en",
      filters: { price_max: 300, brands: ["adidas", "nike", "nike"] },
    };
    expect(decodePageToken(token, reordered, { secret: SECRET, now: NOW })).toEqual(CURSOR);
  });

  it("Arabic queries and cursors with non-ASCII search_after values", () => {
    const cursor: Cursor = { ...CURSOR, search_after: [3.25, "حذاء-جري"] };
    const query: PageTokenQuery = { q: "حذاء جري اسود", lang: "ar", filters: { colours: ["black"] } };
    const token = issue(cursor, query);
    expect(decodePageToken(token, query, { secret: SECRET, now: NOW })).toEqual(cursor);
  });
});

describe("decodePageToken rejects tampering", () => {
  it("a flipped byte anywhere in the body", () => {
    const token = issue();
    const [body, sig] = split(token);
    for (const i of [0, Math.floor(body.length / 2), body.length - 1]) {
      expectReason(() => decodePageToken(`${flipChar(body, i)}.${sig}`, QUERY, { secret: SECRET, now: NOW }), "bad_signature");
    }
  });

  it("a flipped byte anywhere in the signature", () => {
    const [body, sig] = split(issue());
    // The last base64url char of a 32-byte MAC carries padding bits; flip full-byte positions only.
    for (const i of [0, 10, sig.length - 2]) {
      expectReason(() => decodePageToken(`${body}.${flipChar(sig, i)}`, QUERY, { secret: SECRET, now: NOW }), "bad_signature");
    }
  });

  it("an edited cursor re-encoded under the original signature (forged PIT / window)", () => {
    const [body, sig] = split(issue());
    const payload = JSON.parse(Buffer.from(body, "base64url").toString("utf8")) as { c: Cursor };
    payload.c = { ...payload.c, window_pos: 0, pit_id: "attacker-pit" };
    const forged = Buffer.from(JSON.stringify(payload), "utf8").toString("base64url");
    expectReason(() => decodePageToken(`${forged}.${sig}`, QUERY, { secret: SECRET, now: NOW }), "bad_signature");
  });

  it("an extended expiry re-encoded under the original signature", () => {
    const [body, sig] = split(issue());
    const payload = JSON.parse(Buffer.from(body, "base64url").toString("utf8")) as { exp: number };
    payload.exp += 86_400;
    const forged = Buffer.from(JSON.stringify(payload), "utf8").toString("base64url");
    expectReason(() => decodePageToken(`${forged}.${sig}`, QUERY, { secret: SECRET, now: NOW }), "bad_signature");
  });

  it("a token signed with a different secret", () => {
    const token = issue(CURSOR, QUERY, "some-other-secret-some-other-secret!!");
    expectReason(() => decodePageToken(token, QUERY, { secret: SECRET, now: NOW }), "bad_signature");
  });

  it("swapping signatures between two valid tokens", () => {
    const [bodyA] = split(issue());
    const [, sigB] = split(issue({ ...CURSOR, window_pos: 48 }));
    expectReason(() => decodePageToken(`${bodyA}.${sigB}`, QUERY, { secret: SECRET, now: NOW }), "bad_signature");
  });
});

describe("decodePageToken rejects expiry and query mismatch", () => {
  it("an expired token", () => {
    const token = issue();
    expectReason(() => decodePageToken(token, QUERY, { secret: SECRET, now: NOW + TTL_S * 1000 }), "expired");
    expectReason(() => decodePageToken(token, QUERY, { secret: SECRET, now: NOW + 3_600_000 }), "expired");
  });

  it.each<[string, PageTokenQuery]>([
    ["different q", { ...QUERY, q: "white running shoes" }],
    ["different lang", { ...QUERY, lang: "ar" }],
    ["auto instead of en", { ...QUERY, lang: "auto" }],
    ["an added filter", { ...QUERY, filters: { ...QUERY.filters, colours: ["black"] } }],
    ["a removed filter", { ...QUERY, filters: { brands: ["nike", "adidas"] } }],
    ["a changed price", { ...QUERY, filters: { brands: ["nike", "adidas"], price_max: 301 } }],
    ["no filters", { q: QUERY.q, lang: QUERY.lang }],
  ])("a token replayed against %s", (_name, other) => {
    expectReason(() => decodePageToken(issue(), other, { secret: SECRET, now: NOW }), "query_mismatch");
  });

  it("checks the signature before expiry (an expired forgery is still a bad signature)", () => {
    const [body, sig] = split(issue());
    expectReason(
      () => decodePageToken(`${flipChar(body, 3)}.${sig}`, QUERY, { secret: SECRET, now: NOW + 3_600_000 }),
      "bad_signature",
    );
  });
});

describe("decodePageToken rejects malformed input", () => {
  it.each([
    ["empty", ""],
    ["no separator", "abcdef"],
    ["three parts", "a.b.c"],
    ["empty signature", "abc."],
    ["empty body", ".abc"],
    ["non-base64url characters", "ab+c/d=.ef$g"],
    ["oversized", `${"A".repeat(5000)}.${"B".repeat(43)}`],
  ])("%s", (_name, token) => {
    expectReason(() => decodePageToken(token, QUERY, { secret: SECRET, now: NOW }), "malformed");
  });

  it("a correctly signed body that is not a token payload", () => {
    for (const raw of ["not json", "[]", JSON.stringify({ c: "x", qh: 1, exp: "soon" })]) {
      const body = Buffer.from(raw, "utf8").toString("base64url");
      const sig = createHmac("sha256", SECRET).update(body).digest("base64url");
      expectReason(() => decodePageToken(`${body}.${sig}`, QUERY, { secret: SECRET, now: NOW }), "malformed");
    }
  });

  it("every rejection carries a human-readable message and no secret material", () => {
    const err = rejection(() => decodePageToken("a.b.c", QUERY, { secret: SECRET, now: NOW }));
    expect(err.message.length).toBeGreaterThan(0);
    expect(err.message).not.toContain(SECRET);
  });
});

describe("encodePageToken", () => {
  it("refuses an empty secret", () => {
    expect(() => encodePageToken(CURSOR, QUERY, { secret: "", ttlSeconds: TTL_S, now: NOW })).toThrow();
  });

  it("issues different tokens for different cursors of the same query", () => {
    expect(issue({ ...CURSOR, window_pos: 48 })).not.toBe(issue());
  });
});

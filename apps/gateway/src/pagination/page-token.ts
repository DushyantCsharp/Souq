// Public page tokens (CLAUDE.md "Public page token"):
//   base64url(JSON{c: Cursor, qh: sha256(q|lang|filters canonical), exp}) + "." + base64url(HMAC-SHA256(secret, body))
// The cursor stays opaque; the HMAC stops clients forging PIT ids/search_after values, `exp` bounds token
// life to the PIT keep-alive, and `qh` stops a token from one query being replayed against another.
import { createHmac, timingSafeEqual } from "node:crypto";

import { canonicalFilters, canonicalJson, canonicalLang, canonicalQuery, sha256 } from "../search/canonical.js";
import type { Cursor } from "../search/types.js";

export type InvalidPageTokenReason = "malformed" | "bad_signature" | "expired" | "query_mismatch" | "stale";

const MESSAGES: Record<InvalidPageTokenReason, string> = {
  malformed: "The page token is malformed.",
  bad_signature: "The page token signature is invalid.",
  expired: "The page token has expired; restart the search.",
  query_mismatch: "The page token was issued for a different query.",
  // Signed and unexpired, but services/search could no longer resume from it (snapshot gone).
  stale: "The page token no longer points to a live result set; restart the search.",
};

export class InvalidPageTokenError extends Error {
  readonly reason: InvalidPageTokenReason;

  constructor(reason: InvalidPageTokenReason) {
    super(MESSAGES[reason]);
    this.name = "InvalidPageTokenError";
    this.reason = reason;
  }
}

/** The request a token is bound to. `filters` are the *explicit* request filters. */
export interface PageTokenQuery {
  q: string;
  lang?: string | null;
  filters?: Readonly<Record<string, unknown>> | null;
}

export interface EncodePageTokenOptions {
  secret: string;
  /** Token lifetime; the gateway passes the PIT keep-alive from budgets.yaml. */
  ttlSeconds: number;
  /** Clock override for tests (epoch ms). */
  now?: number;
}

export interface DecodePageTokenOptions {
  secret: string;
  now?: number;
}

interface TokenBody {
  c: Cursor;
  qh: string;
  exp: number;
}

const BASE64URL = /^[A-Za-z0-9_-]+$/;
// Generous upper bound so a hostile token cannot make us hash/parse megabytes.
const MAX_TOKEN_LENGTH = 4096;

/** sha256 over `q|lang|filters` in canonical form. */
export function pageTokenQueryHash(query: PageTokenQuery): string {
  return sha256(
    `${canonicalQuery(query.q)}|${canonicalLang(query.lang)}|${canonicalJson(canonicalFilters(query.filters))}`,
  );
}

function sign(secret: string, body: string): Buffer {
  if (secret.length === 0) throw new TypeError("page token secret must not be empty");
  return createHmac("sha256", secret).update(body, "utf8").digest();
}

export function encodePageToken(cursor: Cursor, query: PageTokenQuery, options: EncodePageTokenOptions): string {
  const now = options.now ?? Date.now();
  const payload: TokenBody = {
    c: cursor,
    qh: pageTokenQueryHash(query),
    exp: Math.floor(now / 1000) + Math.max(1, Math.floor(options.ttlSeconds)),
  };
  const body = Buffer.from(JSON.stringify(payload), "utf8").toString("base64url");
  return `${body}.${sign(options.secret, body).toString("base64url")}`;
}

function constantTimeEqual(a: Buffer, b: Buffer): boolean {
  return a.length === b.length && timingSafeEqual(a, b);
}

/**
 * Verifies signature, expiry and query binding (in that order: nothing attacker-controlled is parsed
 * before the HMAC checks out) and returns the cursor to forward to services/search.
 */
export function decodePageToken(token: string, query: PageTokenQuery, options: DecodePageTokenOptions): Cursor {
  if (typeof token !== "string" || token.length === 0 || token.length > MAX_TOKEN_LENGTH) {
    throw new InvalidPageTokenError("malformed");
  }
  const parts = token.split(".");
  const [body, signature] = parts;
  if (parts.length !== 2 || !body || !signature || !BASE64URL.test(body) || !BASE64URL.test(signature)) {
    throw new InvalidPageTokenError("malformed");
  }
  if (!constantTimeEqual(Buffer.from(signature, "base64url"), sign(options.secret, body))) {
    throw new InvalidPageTokenError("bad_signature");
  }

  let payload: Partial<TokenBody>;
  try {
    payload = JSON.parse(Buffer.from(body, "base64url").toString("utf8")) as Partial<TokenBody>;
  } catch {
    throw new InvalidPageTokenError("malformed");
  }
  const { c, qh, exp } = payload ?? {};
  if (!c || typeof c !== "object" || Array.isArray(c) || typeof qh !== "string" || typeof exp !== "number") {
    throw new InvalidPageTokenError("malformed");
  }

  const nowSeconds = Math.floor((options.now ?? Date.now()) / 1000);
  if (exp <= nowSeconds) throw new InvalidPageTokenError("expired");

  if (!constantTimeEqual(Buffer.from(qh, "utf8"), Buffer.from(pageTokenQueryHash(query), "utf8"))) {
    throw new InvalidPageTokenError("query_mismatch");
  }
  return c;
}

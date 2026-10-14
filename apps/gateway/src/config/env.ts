import { randomBytes } from "node:crypto";

import { z } from "zod";

const csv = z
  .string()
  .default("")
  .transform((value) =>
    value
      .split(",")
      .map((part) => part.trim())
      .filter((part) => part.length > 0),
  );

const EnvSchema = z.object({
  NODE_ENV: z.string().default("development"),
  HOST: z.string().default("0.0.0.0"),
  PORT: z.coerce.number().int().min(0).max(65535).default(4000),
  LOG_LEVEL: z.enum(["fatal", "error", "warn", "info", "debug", "trace", "silent"]).default("info"),
  SEARCH_URL: z.url().default("http://localhost:8001"),
  FEEDS_URL: z.url().default("http://localhost:8002"),
  // Empty disables Redis entirely (no response cache, in-memory rate limiting).
  REDIS_URL: z.string().default("redis://localhost:6379/0"),
  PAGE_TOKEN_SECRET: z.string().optional(),
  GATEWAY_API_KEYS: csv,
  RATE_LIMIT_MAX: z.coerce.number().int().positive().default(120),
  RATE_LIMIT_WINDOW: z.string().default("1 minute"),
  // Only trust X-Forwarded-For from known proxies: request.ip keys the per-IP rate limit, and a client
  // that can set XFF freely could otherwise rotate it to dodge the limit. "false" | "true" | CIDR/IP list.
  TRUST_PROXY: z.string().default("false"),
  FEEDS_MAX_UPLOAD_BYTES: z.coerce
    .number()
    .int()
    .positive()
    .default(512 * 1024 * 1024),
});

export interface GatewayEnv {
  nodeEnv: string;
  host: string;
  port: number;
  logLevel: z.infer<typeof EnvSchema>["LOG_LEVEL"];
  searchUrl: string;
  feedsUrl: string;
  redisUrl: string | null;
  pageTokenSecret: string;
  /** True when PAGE_TOKEN_SECRET was missing and a per-process secret was generated. */
  pageTokenSecretEphemeral: boolean;
  apiKeys: string[];
  rateLimit: { max: number; timeWindow: string };
  feedsMaxUploadBytes: number;
  trustProxy: boolean | string[];
}

export function parseTrustProxy(value: string): boolean | string[] {
  const v = value.trim().toLowerCase();
  if (v === "" || v === "false") return false;
  if (v === "true") return true;
  return v
    .split(",")
    .map((part) => part.trim())
    .filter((part) => part.length > 0);
}

// HMAC-SHA256 keys shorter than the digest weaken forgery resistance.
export const MIN_PAGE_TOKEN_SECRET_BYTES = 32;

export function loadEnv(source: NodeJS.ProcessEnv = process.env): GatewayEnv {
  const env = EnvSchema.parse(source);
  const production = env.NODE_ENV === "production";
  let secret = env.PAGE_TOKEN_SECRET?.trim() ?? "";
  let ephemeral = false;
  if (secret.length === 0) {
    if (production) throw new Error("PAGE_TOKEN_SECRET is required when NODE_ENV=production");
    // Dev convenience: tokens stay valid only for this process, which is fine for a single local gateway.
    secret = randomBytes(MIN_PAGE_TOKEN_SECRET_BYTES).toString("base64url");
    ephemeral = true;
  } else if (Buffer.byteLength(secret) < MIN_PAGE_TOKEN_SECRET_BYTES) {
    throw new Error(`PAGE_TOKEN_SECRET must be at least ${MIN_PAGE_TOKEN_SECRET_BYTES} bytes`);
  }
  return {
    nodeEnv: env.NODE_ENV,
    host: env.HOST,
    port: env.PORT,
    logLevel: env.LOG_LEVEL,
    searchUrl: env.SEARCH_URL,
    feedsUrl: env.FEEDS_URL,
    redisUrl: env.REDIS_URL.trim() === "" ? null : env.REDIS_URL,
    pageTokenSecret: secret,
    pageTokenSecretEphemeral: ephemeral,
    apiKeys: env.GATEWAY_API_KEYS,
    rateLimit: { max: env.RATE_LIMIT_MAX, timeWindow: env.RATE_LIMIT_WINDOW },
    feedsMaxUploadBytes: env.FEEDS_MAX_UPLOAD_BYTES,
    trustProxy: parseTrustProxy(env.TRUST_PROXY),
  };
}

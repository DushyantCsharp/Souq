import rateLimit from "@fastify/rate-limit";
import type { FastifyInstance, FastifyRequest } from "fastify";
import type { Redis } from "ioredis";

import { API_KEY_HEADER, type ApiKeyVerifier, headerValue } from "./auth/api-key.js";
import { ProblemError } from "./errors/problem.js";

export interface RateLimitDeps {
  max: number;
  timeWindow: string | number;
  verifier: ApiKeyVerifier;
  /** Shared Redis so limits hold across gateway replicas; omitted -> per-process memory store. */
  redis?: Redis | null;
}

export async function registerRateLimit(app: FastifyInstance, deps: RateLimitDeps): Promise<void> {
  await app.register(rateLimit, {
    global: true,
    max: deps.max,
    timeWindow: deps.timeWindow,
    ...(deps.redis ? { redis: deps.redis } : {}),
    nameSpace: "souq:gw:rl:",
    // Redis down must not take search down with it.
    skipOnError: true,
    // Only a *valid* key gets its own bucket; otherwise rotating junk keys would dodge the per-IP limit.
    keyGenerator: (request: FastifyRequest) => {
      const keyId = deps.verifier.verify(headerValue(request, API_KEY_HEADER));
      return keyId ? `key:${keyId}` : `ip:${request.ip}`;
    },
    errorResponseBuilder: (_request, context) =>
      new ProblemError("rate-limited", 429, {
        detail: `Rate limit of ${context.max} requests exceeded; retry in ${context.after}.`,
        // The plugin already set Retry-After; repeating it keeps the header if a handler resets headers.
        headers: { "retry-after": String(Math.max(1, Math.ceil(context.ttl / 1000))) },
      }),
  });
}

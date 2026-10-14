// API-key auth for feed endpoints. Keys are compared as SHA-256 digests with timingSafeEqual so neither
// key length nor matching prefix leaks through response timing.
import { createHash, timingSafeEqual } from "node:crypto";

import type { FastifyReply, FastifyRequest } from "fastify";

import { ProblemError } from "../errors/problem.js";

export const API_KEY_HEADER = "x-api-key";

export interface ApiKeyVerifier {
  /** Short, non-reversible id of the matching key (for rate-limit buckets and logs), or null. */
  verify(presented: string | undefined): string | null;
  readonly configured: boolean;
}

function digest(value: string): Buffer {
  return createHash("sha256").update(value, "utf8").digest();
}

export function createApiKeyVerifier(keys: readonly string[]): ApiKeyVerifier {
  const digests = keys.filter((k) => k.length > 0).map(digest);
  return {
    configured: digests.length > 0,
    verify(presented) {
      if (!presented) return null;
      const candidate = digest(presented);
      let match: Buffer | null = null;
      // No early exit: every configured key is compared.
      for (const d of digests) {
        if (timingSafeEqual(candidate, d) && match === null) match = d;
      }
      return match ? match.subarray(0, 8).toString("hex") : null;
    },
  };
}

export function headerValue(request: FastifyRequest, name: string): string | undefined {
  const value = request.headers[name];
  return Array.isArray(value) ? value[0] : value;
}

/** onRequest hook: rejects before any body is read, so unauthenticated uploads are never streamed. */
export function requireApiKey(verifier: ApiKeyVerifier) {
  return async (request: FastifyRequest, _reply: FastifyReply): Promise<void> => {
    const keyId = verifier.verify(headerValue(request, API_KEY_HEADER));
    if (keyId === null) {
      throw new ProblemError("unauthorized", 401, {
        detail: verifier.configured
          ? `Provide a valid API key in the ${API_KEY_HEADER} header.`
          : "No API keys are configured on this gateway (GATEWAY_API_KEYS).",
      });
    }
    request.log.debug({ apiKey: keyId }, "api key accepted");
  };
}

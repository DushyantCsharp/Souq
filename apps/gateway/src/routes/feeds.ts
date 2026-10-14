import type { Readable } from "node:stream";

import type { FastifyInstance, FastifyRequest } from "fastify";

import { type ApiKeyVerifier, headerValue, requireApiKey } from "../auth/api-key.js";
import { type InvalidParam, ProblemError, validationProblem } from "../errors/problem.js";
import type { FeedsClient } from "../feeds/client.js";

export interface FeedsRouteDeps {
  feedsClient: FeedsClient;
  verifier: ApiKeyVerifier;
  maxUploadBytes: number;
}

const FEED_ID = /^[A-Za-z0-9_-]{1,128}$/;
const DEFAULT_ERRORS_LIMIT = 100;
const MAX_ERRORS_LIMIT = 500;
const MAX_ERRORS_TOKEN_LENGTH = 1024;

// Validated before forwarding so a crafted id (`..%2F`, query injection) can never reach another
// internal path on the feeds service.
function feedId(request: FastifyRequest): string {
  const { id } = request.params as { id?: string };
  if (!id || !FEED_ID.test(id)) {
    throw validationProblem([{ location: "path", name: "id", detail: "must match ^[A-Za-z0-9_-]{1,128}$" }]);
  }
  return id;
}

export async function registerFeedRoutes(app: FastifyInstance, deps: FeedsRouteDeps): Promise<void> {
  await app.register(async (scope) => {
    scope.addHook("onRequest", requireApiKey(deps.verifier));

    // Hand the raw request stream to the handler instead of parsing multipart: the gateway forwards the
    // body byte-for-byte and never holds a whole feed file in memory.
    scope.addContentTypeParser("multipart/form-data", (_request, payload, done) => {
      done(null, payload);
    });

    scope.post(
      "/v1/feeds",
      {
        onRequest: async (request) => {
          const contentType = headerValue(request, "content-type") ?? "";
          if (!/^multipart\/form-data\s*;/i.test(contentType) || !/boundary=/i.test(contentType)) {
            throw new ProblemError("unsupported-media-type", 415, {
              detail: "Upload the feed as multipart/form-data with a `file` part (and optionally `format`).",
            });
          }
          const declared = Number(headerValue(request, "content-length") ?? Number.NaN);
          if (Number.isFinite(declared) && declared > deps.maxUploadBytes) {
            throw new ProblemError("payload-too-large", 413, {
              detail: `Uploads are limited to ${deps.maxUploadBytes} bytes.`,
            });
          }
        },
      },
      async (request, reply) => {
        const declared = Number(headerValue(request, "content-length") ?? Number.NaN);
        const accepted = await deps.feedsClient.upload(
          {
            body: request.body as Readable,
            contentType: headerValue(request, "content-type") ?? "",
            ...(Number.isFinite(declared) ? { contentLength: declared } : {}),
            maxBytes: deps.maxUploadBytes,
          },
          { requestId: request.id },
        );
        return reply.code(202).header("location", `/v1/feeds/${encodeURIComponent(accepted.id)}`).send(accepted);
      },
    );

    scope.get("/v1/feeds/:id", async (request) => deps.feedsClient.get(feedId(request), { requestId: request.id }));

    scope.get("/v1/feeds/:id/errors", async (request) => {
      const id = feedId(request);
      const query = (request.query ?? {}) as Record<string, unknown>;
      const errors: InvalidParam[] = [];
      let limit = DEFAULT_ERRORS_LIMIT;
      if (query.limit !== undefined && query.limit !== "") {
        const n = Number(query.limit);
        if (!Number.isInteger(n) || n < 1 || n > MAX_ERRORS_LIMIT) {
          errors.push({ location: "query", name: "limit", detail: `must be an integer 1..${MAX_ERRORS_LIMIT}` });
        } else limit = n;
      }
      const token = query.page_token;
      if (token !== undefined && (typeof token !== "string" || token.length > MAX_ERRORS_TOKEN_LENGTH)) {
        errors.push({ location: "query", name: "page_token", detail: "must be a single opaque token" });
      }
      if (errors.length > 0) throw validationProblem(errors);
      return deps.feedsClient.errors(
        id,
        { limit, ...(typeof token === "string" && token !== "" ? { cursor: token } : {}) },
        { requestId: request.id },
      );
    });
  });
}

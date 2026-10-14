// RFC 9457 problem+json for every non-2xx response (CLAUDE.md conventions). Clients branch on `type`.
import type { FastifyError, FastifyInstance, FastifyReply, FastifyRequest } from "fastify";

import { InvalidPageTokenError } from "../pagination/page-token.js";
import { UpstreamError } from "./upstream.js";

export const PROBLEM_TYPE_BASE = "https://souq.dev/problems/";

const TITLES = {
  "validation-error": "Request validation failed",
  "invalid-page-token": "Invalid page token",
  unauthorized: "Missing or invalid API key",
  "not-found": "Not found",
  "payload-too-large": "Payload too large",
  "unsupported-media-type": "Unsupported media type",
  "rate-limited": "Too many requests",
  "upstream-timeout": "Upstream timeout",
  "search-unavailable": "Search unavailable",
  "feeds-unavailable": "Feeds service unavailable",
  "bad-gateway": "Bad gateway",
  internal: "Internal server error",
} as const;

export type ProblemSlug = keyof typeof TITLES;

export interface InvalidParam {
  location: "query" | "path" | "header" | "body";
  name: string;
  detail: string;
}

export interface ProblemBody {
  type: string;
  title: string;
  status: number;
  detail?: string;
  instance?: string;
  request_id?: string;
  errors?: InvalidParam[];
}

export interface ProblemOptions {
  detail?: string;
  errors?: InvalidParam[];
  headers?: Record<string, string>;
  cause?: unknown;
}

export function problemType(slug: ProblemSlug): string {
  return PROBLEM_TYPE_BASE + slug;
}

export class ProblemError extends Error {
  readonly slug: ProblemSlug;
  readonly statusCode: number;
  readonly detail: string | undefined;
  readonly errors: InvalidParam[] | undefined;
  readonly headers: Record<string, string>;

  constructor(slug: ProblemSlug, status: number, options: ProblemOptions = {}) {
    super(options.detail ?? TITLES[slug], options.cause === undefined ? undefined : { cause: options.cause });
    this.name = "ProblemError";
    this.slug = slug;
    this.statusCode = status;
    this.detail = options.detail;
    this.errors = options.errors;
    this.headers = options.headers ?? {};
  }

  toBody(instance?: string, requestId?: string): ProblemBody {
    const body: ProblemBody = { type: problemType(this.slug), title: TITLES[this.slug], status: this.statusCode };
    if (this.detail !== undefined) body.detail = this.detail;
    if (instance !== undefined) body.instance = instance;
    if (requestId !== undefined) body.request_id = requestId;
    if (this.errors !== undefined) body.errors = this.errors;
    return body;
  }
}

export function validationProblem(errors: InvalidParam[]): ProblemError {
  const detail = errors.length === 1 ? "1 invalid parameter" : `${errors.length} invalid parameters`;
  return new ProblemError("validation-error", 400, { detail, errors });
}

function fromUpstream(err: UpstreamError): ProblemError {
  const unavailable = err.service === "search" ? "search-unavailable" : "feeds-unavailable";
  const options = { detail: err.message, cause: err };
  switch (err.failure) {
    case "timeout":
      return new ProblemError("upstream-timeout", 504, options);
    case "unavailable":
      return new ProblemError(unavailable, 503, {
        ...options,
        ...(err.retryAfterS === undefined ? {} : { headers: { "retry-after": String(err.retryAfterS) } }),
      });
    case "bad_response":
      return new ProblemError("bad-gateway", 502, options);
    case "rejected":
      return new ProblemError("validation-error", 400, options);
    case "not_found":
      return new ProblemError("not-found", 404, options);
    case "too_large":
      return new ProblemError("payload-too-large", 413, options);
    case "unsupported_media_type":
      return new ProblemError("unsupported-media-type", 415, options);
  }
}

function fromStatus(status: number, detail: string | undefined, cause: unknown): ProblemError {
  const options = { detail, cause };
  if (status === 401 || status === 403) return new ProblemError("unauthorized", 401, options);
  if (status === 404) return new ProblemError("not-found", 404, options);
  if (status === 413) return new ProblemError("payload-too-large", 413, options);
  if (status === 415) return new ProblemError("unsupported-media-type", 415, options);
  if (status === 429) return new ProblemError("rate-limited", 429, options);
  if (status >= 400 && status < 500) return new ProblemError("validation-error", 400, options);
  return new ProblemError("internal", 500, { cause });
}

/** Maps anything thrown inside a request to the problem it should be reported as. */
export function toProblemError(err: unknown): ProblemError {
  if (err instanceof ProblemError) return err;
  if (err instanceof InvalidPageTokenError) {
    return new ProblemError("invalid-page-token", 400, { detail: err.message, cause: err });
  }
  if (err instanceof UpstreamError) return fromUpstream(err);
  const fastifyErr = err as Partial<FastifyError> | undefined;
  if (fastifyErr && typeof fastifyErr === "object") {
    if (Array.isArray(fastifyErr.validation)) {
      return validationProblem(
        fastifyErr.validation.map((v) => ({
          location: fastifyErr.validationContext === "params" ? "path" : "query",
          name: String(v.instancePath || v.params?.missingProperty || "request"),
          detail: v.message ?? "invalid",
        })),
      );
    }
    if (typeof fastifyErr.statusCode === "number") {
      // Fastify's own 4xx messages (bad JSON, missing boundary, ...) are safe to echo; 5xx messages are not.
      const detail = fastifyErr.statusCode < 500 ? fastifyErr.message : undefined;
      return fromStatus(fastifyErr.statusCode, detail, err);
    }
  }
  return new ProblemError("internal", 500, { cause: err });
}

/** Path without the query string: query text is PII and must not leak into problem bodies or logs. */
export function requestPath(url: string): string {
  const q = url.indexOf("?");
  return q === -1 ? url : url.slice(0, q);
}

export function sendProblem(request: FastifyRequest, reply: FastifyReply, problem: ProblemError): FastifyReply {
  // A Buffer payload keeps the media type exactly `application/problem+json` (Fastify appends a charset
  // parameter to string payloads, which +json types do not define).
  return reply
    .code(problem.statusCode)
    .headers(problem.headers)
    .type("application/problem+json")
    .send(Buffer.from(JSON.stringify(problem.toBody(requestPath(request.url), request.id)), "utf8"));
}

export function registerProblemHandlers(app: FastifyInstance): void {
  app.setErrorHandler((err, request, reply) => {
    const problem = toProblemError(err);
    if (problem.statusCode >= 500) {
      request.log.error({ err, problem: problem.slug }, "request failed");
    } else {
      request.log.info({ problem: problem.slug, status: problem.statusCode }, "request rejected");
    }
    return sendProblem(request, reply, problem);
  });
  app.setNotFoundHandler((request, reply) =>
    sendProblem(
      request,
      reply,
      new ProblemError("not-found", 404, { detail: `No route for ${request.method} ${requestPath(request.url)}` }),
    ),
  );
}

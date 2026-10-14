// Failures talking to services/search or services/feeds, classified so the edge can pick 4xx/502/503/504.
export type UpstreamService = "search" | "feeds";

export type UpstreamFailure =
  | "timeout"
  | "unavailable"
  | "bad_response"
  | "rejected"
  | "not_found"
  | "too_large"
  | "unsupported_media_type";

export class UpstreamError extends Error {
  readonly service: UpstreamService;
  readonly failure: UpstreamFailure;
  readonly status: number | undefined;
  /** Upstream's Retry-After (seconds) when it shed load; forwarded so clients back off instead of retrying hot. */
  readonly retryAfterS: number | undefined;

  constructor(
    service: UpstreamService,
    failure: UpstreamFailure,
    message: string,
    options: { status?: number; cause?: unknown; retryAfterS?: number } = {},
  ) {
    super(message, options.cause === undefined ? undefined : { cause: options.cause });
    this.name = "UpstreamError";
    this.service = service;
    this.failure = failure;
    this.status = options.status;
    this.retryAfterS = options.retryAfterS;
  }
}

/** Retry-After in delta-seconds form (the only form services/search sends); anything else is ignored. */
export function parseRetryAfter(value: string | null | undefined): number | undefined {
  if (!value || !/^\d+$/.test(value.trim())) return undefined;
  return Math.max(1, Number(value.trim()));
}

// Checked by name, not instanceof: fetch/AbortSignal errors are DOMExceptions created by Node internals,
// which are not `instanceof Error` across realms (e.g. Jest's VM sandbox).
function errorName(err: unknown): string | undefined {
  return typeof err === "object" && err !== null ? String((err as { name?: unknown }).name) : undefined;
}

export function isTimeout(err: unknown): boolean {
  return errorName(err) === "TimeoutError";
}

export function isAbort(err: unknown): boolean {
  return errorName(err) === "AbortError";
}

/** Fetch rejected before a response arrived: timeout, caller abort, or the service is unreachable. */
export function classifyFetchFailure(service: UpstreamService, err: unknown): Error {
  if (isTimeout(err)) return new UpstreamError(service, "timeout", `${service} service exceeded its latency budget`);
  // The caller went away (client disconnect); nothing useful to report upstream-wise.
  if (isAbort(err)) return err as Error;
  if (errorName(err) === "SyntaxError") {
    return new UpstreamError(service, "bad_response", `${service} service returned malformed JSON`, { cause: err });
  }
  return new UpstreamError(service, "unavailable", `${service} service is unreachable`, { cause: err });
}

/** Non-2xx status from a service. */
export function classifyStatus(
  service: UpstreamService,
  status: number,
  detail?: string,
  retryAfterS?: number,
): UpstreamError {
  const suffix = detail ? `: ${detail}` : "";
  if (status === 404) return new UpstreamError(service, "not_found", `not found${suffix}`, { status });
  if (status === 413) return new UpstreamError(service, "too_large", `payload too large${suffix}`, { status });
  if (status === 415) {
    return new UpstreamError(service, "unsupported_media_type", `unsupported media type${suffix}`, { status });
  }
  if (status === 400 || status === 422) {
    return new UpstreamError(service, "rejected", `${service} service rejected the request${suffix}`, { status });
  }
  if (status === 502 || status === 503 || status === 504) {
    return new UpstreamError(service, "unavailable", `${service} service returned ${status}`, {
      status,
      ...(retryAfterS === undefined ? {} : { retryAfterS }),
    });
  }
  if (status >= 500) {
    return new UpstreamError(service, "unavailable", `${service} service failed with ${status}`, { status });
  }
  return new UpstreamError(service, "bad_response", `${service} service returned unexpected ${status}`, { status });
}

/** Human-readable summary of a FastAPI-style error body (`{detail: str | [{loc, msg}]}`). */
export function describeErrorBody(body: unknown): string | undefined {
  if (!body || typeof body !== "object") return undefined;
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const parts = detail
      .map((d: unknown) => {
        if (!d || typeof d !== "object") return undefined;
        const { loc, msg } = d as { loc?: unknown; msg?: unknown };
        const where = Array.isArray(loc) ? loc.join(".") : undefined;
        return typeof msg === "string" ? (where ? `${where}: ${msg}` : msg) : undefined;
      })
      .filter((p): p is string => p !== undefined);
    return parts.length > 0 ? parts.join("; ") : undefined;
  }
  return undefined;
}

/** Combines the per-call budget with an optional caller signal (client disconnect). */
export function budgetSignal(timeoutMs: number, caller?: AbortSignal): AbortSignal {
  const timeout = AbortSignal.timeout(timeoutMs);
  return caller ? AbortSignal.any([timeout, caller]) : timeout;
}

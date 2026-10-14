import { describe, expect, it } from "@jest/globals";

import { toProblemError } from "../src/errors/problem.js";
import { classifyStatus, parseRetryAfter, UpstreamError } from "../src/errors/upstream.js";

describe("load-shedding Retry-After passthrough", () => {
  it("parses delta-seconds and ignores anything else", () => {
    expect(parseRetryAfter("3")).toBe(3);
    expect(parseRetryAfter(" 0 ")).toBe(1);
    expect(parseRetryAfter("Wed, 21 Oct 2026 07:28:00 GMT")).toBeUndefined();
    expect(parseRetryAfter(null)).toBeUndefined();
  });

  it("keeps the upstream Retry-After on a shed 503", () => {
    const err = classifyStatus("search", 503, "too many requests in flight", 2);
    expect(err.failure).toBe("unavailable");
    expect(err.retryAfterS).toBe(2);
    const problem = toProblemError(err);
    expect(problem.statusCode).toBe(503);
    expect(problem.slug).toBe("search-unavailable");
    expect(problem.headers).toEqual({ "retry-after": "2" });
  });

  it("sends no Retry-After when search gave none", () => {
    const problem = toProblemError(new UpstreamError("search", "unavailable", "search service returned 503"));
    expect(problem.headers).toEqual({});
  });
});

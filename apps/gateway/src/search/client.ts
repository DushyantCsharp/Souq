// The gateway's only route to search: a client generated from the services/search OpenAPI spec
// (CLAUDE.md rule 1 — never OpenSearch directly).
import type { paths } from "@souq/contracts/search-internal";
import createClient from "openapi-fetch";

import {
  budgetSignal,
  classifyFetchFailure,
  classifyStatus,
  describeErrorBody,
  parseRetryAfter,
  UpstreamError,
} from "../errors/upstream.js";
import type { InternalSearchRequest, InternalSearchResponse } from "./types.js";

export interface SearchCallOptions {
  /** Caller cancellation (e.g. client disconnect); the latency budget is applied by the client. */
  signal?: AbortSignal;
  requestId?: string;
}

/** Injectable seam: routes and GraphQL resolvers only ever see this interface. */
export interface SearchClient {
  search(request: InternalSearchRequest, options?: SearchCallOptions): Promise<InternalSearchResponse>;
}

export interface SearchClientOptions {
  baseUrl: string;
  /** `gateway.search_timeout_ms` from budgets.yaml. */
  timeoutMs: number;
  fetch?: typeof globalThis.fetch;
}

function looksLikeSearchResponse(data: unknown): data is InternalSearchResponse {
  if (!data || typeof data !== "object") return false;
  const d = data as Record<string, unknown>;
  return Array.isArray(d.items) && typeof d.parsed_query === "object" && d.parsed_query !== null;
}

export function createSearchClient(options: SearchClientOptions): SearchClient {
  const client = createClient<paths>({
    baseUrl: options.baseUrl,
    ...(options.fetch ? { fetch: options.fetch } : {}),
  });

  return {
    async search(request, callOptions = {}) {
      let result;
      try {
        result = await client.POST("/search", {
          body: request,
          signal: budgetSignal(options.timeoutMs, callOptions.signal),
          headers: callOptions.requestId ? { "x-request-id": callOptions.requestId } : {},
        });
      } catch (err) {
        throw classifyFetchFailure("search", err);
      }
      const { data, error, response } = result;
      if (!response.ok) {
        const retryAfter = parseRetryAfter(response.headers.get("retry-after"));
        throw classifyStatus("search", response.status, describeErrorBody(error), retryAfter);
      }
      if (!looksLikeSearchResponse(data)) {
        throw new UpstreamError("search", "bad_response", "search service returned an unexpected body", {
          status: response.status,
        });
      }
      return data;
    },
  };
}

/** Readiness probe against services/search `GET /readyz`. */
export function createSearchProbe(options: SearchClientOptions): () => Promise<boolean> {
  const client = createClient<paths>({
    baseUrl: options.baseUrl,
    ...(options.fetch ? { fetch: options.fetch } : {}),
  });
  return async () => {
    try {
      const { response } = await client.GET("/readyz", { signal: AbortSignal.timeout(options.timeoutMs) });
      return response.ok;
    } catch {
      return false;
    }
  };
}

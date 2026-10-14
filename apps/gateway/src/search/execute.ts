// The one search pipeline behind both REST /v1/search and GraphQL Query.search:
// page token -> cache -> services/search -> public response (+ next page token) -> cache.
// Items are passed through in the order services/search returned them (CLAUDE.md rule 2).
import type { Budgets } from "../config/budgets.js";
import { searchCacheKey } from "../cache/keys.js";
import type { ResponseCache } from "../cache/response-cache.js";
import { UpstreamError } from "../errors/upstream.js";
import {
  decodePageToken,
  encodePageToken,
  InvalidPageTokenError,
  type PageTokenQuery,
} from "../pagination/page-token.js";
import type { SearchCallOptions, SearchClient } from "./client.js";
import type {
  Cursor,
  Filters,
  InternalFilters,
  InternalSearchRequest,
  InternalSearchResponse,
  PublicSearchItem,
  PublicSearchResponse,
  SearchInput,
} from "./types.js";

export type CacheStatus = "hit" | "miss" | "bypass";

export interface SearchDeps {
  searchClient: SearchClient;
  pageTokenSecret: string;
  budgets: Budgets;
  cache?: ResponseCache | null;
  log?: { warn(obj: object, msg: string): void };
  /**
   * Called once per search with the canonical query, e.g. to put a keyed query hash on the active span.
   * Injected by server.ts so library code never imports the telemetry bootstrap.
   */
  annotate?: (q: string) => void;
}

export interface SearchOutcome {
  response: PublicSearchResponse;
  cache: CacheStatus;
}

/** Internal Filters (nullable fields) -> public Filters (absent fields). */
export function toPublicFilters(filters: InternalFilters | null | undefined): Filters {
  const out: Filters = {};
  if (!filters) return out;
  if (filters.price_min != null) out.price_min = filters.price_min;
  if (filters.price_max != null) out.price_max = filters.price_max;
  if (filters.currency != null) out.currency = filters.currency;
  if (filters.brands?.length) out.brands = filters.brands;
  if (filters.colours?.length) out.colours = filters.colours;
  if (filters.sizes?.length) out.sizes = filters.sizes;
  if (filters.category != null) out.category = filters.category;
  return out;
}

function toPublicItem(item: InternalSearchResponse["items"][number]): PublicSearchItem {
  return {
    sku: item.sku,
    title: item.title,
    description: item.description ?? null,
    brand: item.brand,
    price: { amount: item.price.amount, currency: item.price.currency },
    category_path: item.category_path,
    colour: item.colour ?? null,
    size: item.size ?? null,
    score: item.score,
    rank_source: item.rank_source,
  };
}

function toPublicResponse(internal: InternalSearchResponse, nextPageToken: string | null): PublicSearchResponse {
  const pq = internal.parsed_query;
  return {
    items: internal.items.map(toPublicItem),
    facets: (internal.facets ?? []).map((f) => ({
      name: f.name,
      buckets: f.buckets.map((b) => ({ value: b.value, count: b.count })),
    })),
    applied_filters: toPublicFilters(internal.applied_filters),
    parsed_query: {
      original: pq.original,
      normalised: pq.normalised,
      detected_lang: pq.detected_lang,
      script: pq.script,
      rewritten: pq.rewritten ?? null,
      filters: toPublicFilters(pq.filters),
      parser: pq.parser,
      unparsed_tokens: pq.unparsed_tokens ?? [],
    },
    degraded: [...(internal.degraded ?? [])],
    next_page_token: nextPageToken,
    total_estimate: internal.total_estimate ?? internal.items.length,
  };
}

function hasFilters(filters: Filters): boolean {
  return Object.keys(filters).length > 0;
}

function parseCached(raw: string | null): PublicSearchResponse | null {
  if (raw === null) return null;
  try {
    return JSON.parse(raw) as PublicSearchResponse;
  } catch {
    return null;
  }
}

/** TTL for a fresh response. Degraded responses get the (optional) short TTL or are not cached. */
export function cacheTtlSeconds(response: PublicSearchResponse, budgets: Budgets): number {
  if (response.degraded.length > 0) return budgets.gateway.degraded_response_cache_ttl_s ?? 0;
  return budgets.gateway.response_cache_ttl_s;
}

export async function executeSearch(
  deps: SearchDeps,
  input: SearchInput,
  options: SearchCallOptions = {},
): Promise<SearchOutcome> {
  deps.annotate?.(input.q);
  const tokenQuery: PageTokenQuery = { q: input.q, lang: input.lang, filters: input.filters };
  // Throws InvalidPageTokenError before any cache or upstream work for forged/expired/mismatched tokens.
  const cursor: Cursor | undefined = input.pageToken
    ? decodePageToken(input.pageToken, tokenQuery, { secret: deps.pageTokenSecret })
    : undefined;

  const key = searchCacheKey({
    q: input.q,
    lang: input.lang,
    filters: input.filters,
    limit: input.limit,
    page_token: input.pageToken,
  });
  if (deps.cache) {
    const hit = parseCached(await deps.cache.get(key));
    if (hit) return { response: hit, cache: "hit" };
  }

  const request: InternalSearchRequest = { q: input.q, lang: input.lang, limit: input.limit };
  if (hasFilters(input.filters)) request.filters = input.filters;
  if (cursor) request.cursor = cursor;

  let internal: InternalSearchResponse;
  try {
    internal = await deps.searchClient.search(request, options);
  } catch (err) {
    // services/search answers 400 (invalid-cursor) when a correctly signed cursor no longer resolves,
    // e.g. its PIT was closed early. To the client that is just a page token to restart from.
    if (cursor && err instanceof UpstreamError && err.status === 400) throw new InvalidPageTokenError("stale");
    throw err;
  }
  if (!Array.isArray(internal.items) || !internal.parsed_query) {
    throw new UpstreamError("search", "bad_response", "search service returned an incomplete response");
  }

  const nextPageToken = internal.next_cursor
    ? encodePageToken(internal.next_cursor, tokenQuery, {
        secret: deps.pageTokenSecret,
        ttlSeconds: deps.budgets.pageTokenTtlSeconds,
      })
    : null;
  const response = toPublicResponse(internal, nextPageToken);

  if (!deps.cache) return { response, cache: "bypass" };
  const ttl = cacheTtlSeconds(response, deps.budgets);
  if (ttl > 0) {
    // Not awaited: the write must not add latency to the response.
    deps.cache.set(key, JSON.stringify(response), ttl).catch((err: unknown) => {
      deps.log?.warn({ err }, "response cache write failed");
    });
  }
  return { response, cache: "miss" };
}

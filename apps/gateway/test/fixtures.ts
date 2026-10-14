// Shared fakes for gateway tests (not a test file itself: testMatch only picks up *.test.ts).
import type { FastifyInstance } from "fastify";

import { buildApp, type AppDeps } from "../src/app.js";
import type { ResponseCache } from "../src/cache/response-cache.js";
import { type Budgets, parseBudgets } from "../src/config/budgets.js";
import { type GatewayEnv, loadEnv } from "../src/config/env.js";
import type { FeedsClient } from "../src/feeds/client.js";
import type { SearchCallOptions, SearchClient } from "../src/search/client.js";
import type { Cursor, InternalSearchRequest, InternalSearchResponse } from "../src/search/types.js";

export const TEST_SECRET = "test-secret-test-secret-test-secret!!";

export const TEST_BUDGETS: Budgets = parseBudgets(`
search:
  pit_keep_alive: 5m
gateway:
  search_timeout_ms: 200
  feeds_timeout_ms: 500
  response_cache_ttl_s: 60
`);

export function testEnv(overrides: Partial<GatewayEnv> = {}): GatewayEnv {
  return {
    ...loadEnv({ PAGE_TOKEN_SECRET: TEST_SECRET, GATEWAY_API_KEYS: "key-one,key-two", NODE_ENV: "test" }),
    ...overrides,
  };
}

export const CURSOR: Cursor = { query_key: "qk-1", pit_id: "pit-abc", window_pos: 24, search_after: [1.5, "B000123"] };

export function searchResponse(overrides: Partial<InternalSearchResponse> = {}): InternalSearchResponse {
  return {
    items: [
      {
        sku: "B000123",
        title: "Nike Air Zoom",
        description: null,
        brand: "Nike",
        price: { amount: 249.5, currency: "AED" },
        category_path: "Fashion > Shoes > Running",
        colour: "red",
        size: null,
        score: 0.91,
        rank_source: "rerank",
      },
      {
        sku: "B000456",
        title: "Nike Pegasus",
        description: "Daily trainer",
        brand: "Nike",
        price: { amount: 199, currency: "AED" },
        category_path: "Fashion > Shoes > Running",
        colour: "red",
        size: "42",
        score: 0.75,
        rank_source: "fused",
      },
    ],
    facets: [{ name: "brand", buckets: [{ value: "Nike", count: 2 }] }],
    applied_filters: { brands: ["Nike"], colours: ["red"], price_max: 300 },
    parsed_query: {
      original: "red nike shoes under 300",
      normalised: "red nike shoes under 300",
      detected_lang: "en",
      script: "en",
      rewritten: "nike shoes",
      filters: { colours: ["red"], price_max: 300 },
      parser: "rule",
      unparsed_tokens: [],
    },
    degraded: [],
    next_cursor: CURSOR,
    total_estimate: 2,
    timings_ms: { total: 12.5 },
    ...overrides,
  };
}

export interface FakeSearchClient extends SearchClient {
  calls: { request: InternalSearchRequest; options: SearchCallOptions | undefined }[];
}

/** Records every call; `impl` decides the outcome (default: the fixture response). */
export function fakeSearchClient(
  impl: (request: InternalSearchRequest) => Promise<InternalSearchResponse> = async () => searchResponse(),
): FakeSearchClient {
  const calls: FakeSearchClient["calls"] = [];
  return {
    calls,
    async search(request, options) {
      calls.push({ request, options });
      return impl(request);
    },
  };
}

export function unusedFeedsClient(): FeedsClient {
  const fail = (): never => {
    throw new Error("feeds client not expected in this test");
  };
  return { upload: fail, get: fail, errors: fail };
}

export async function testApp(overrides: Partial<AppDeps> = {}): Promise<FastifyInstance> {
  const app = await buildApp({
    env: testEnv(),
    budgets: TEST_BUDGETS,
    searchClient: fakeSearchClient(),
    feedsClient: unusedFeedsClient(),
    logger: false,
    ...overrides,
  });
  await app.ready();
  return app;
}

export type { ResponseCache };

// GraphQL resolvers executed through the real schema (typeDefs + resolvers) against a mocked search client.
// Checks the Relay connection shape, that cursors are signed page tokens wrapping the search service's
// opaque cursor, that `lang` reaches the service (which localizes titles), that degradations and item
// order pass straight through (the gateway never re-ranks, CLAUDE.md rule 2), and that bad `after`
// values fail as INVALID_PAGE_TOKEN without touching the search service.
import { describe, expect, it, jest } from "@jest/globals";
import { type ExecutionResult, graphql } from "graphql";
import { createSchema } from "graphql-yoga";

import { parseBudgets } from "../src/config/budgets.js";
import { typeDefs } from "../src/generated/schema.js";
import type { GraphQLContext } from "../src/graphql/context.js";
import { resolvers } from "../src/graphql/resolvers.js";
import { decodePageToken, encodePageToken } from "../src/pagination/page-token.js";
import type { SearchCallOptions, SearchClient } from "../src/search/client.js";
import type { Cursor, InternalSearchItem, InternalSearchRequest, InternalSearchResponse } from "../src/search/types.js";

const SECRET = "resolver-test-secret-0123456789abcdef!!";
const BUDGETS = parseBudgets(`
search:
  pit_keep_alive: 5m
gateway:
  search_timeout_ms: 1400
  feeds_timeout_ms: 10000
  response_cache_ttl_s: 60
`);

const schema = createSchema<GraphQLContext>({ typeDefs, resolvers });

const SEARCH = /* GraphQL */ `
  query Search($input: SearchInput!, $first: Int, $after: String) {
    search(input: $input, first: $first, after: $after) {
      totalEstimate
      degraded
      edges {
        cursor
        node {
          sku
          title
          description
          brand
          price { amount currency }
          categoryPath
          colour
          size
          score
          rankSource
        }
      }
      pageInfo { hasNextPage hasPreviousPage startCursor endCursor }
      facets { name buckets { value count } }
      appliedFilters { priceMin priceMax currency brands colours sizes category }
      interpretation {
        original
        normalised
        rewritten
        detectedLang
        script
        parser
        unparsedTokens
        filters { priceMin priceMax brands colours sizes }
        filterSources { field value source }
      }
    }
  }
`;

const NEXT_CURSOR: Cursor = { query_key: "qk-1", pit_id: "pit-abc", window_pos: 24, search_after: [1.5, "B2"] };

interface Bilingual {
  sku: string;
  en: string;
  ar: string;
  score: number;
  rank: InternalSearchItem["rank_source"];
}

const CATALOG: Bilingual[] = [
  { sku: "B1", en: "Sony wireless headphones", ar: "سماعات سوني لاسلكية", score: 0.42, rank: "rerank" },
  { sku: "B2", en: "Bose headphones", ar: "سماعات بوز", score: 0.97, rank: "rerank" },
  { sku: "B3", en: "JBL earbuds", ar: "سماعات جي بي ال", score: 0.11, rank: "fused" },
];

/** Localizes like services/search does: by request lang, `auto` resolving to the detected language. */
function respond(request: InternalSearchRequest, overrides: Partial<InternalSearchResponse> = {}): InternalSearchResponse {
  const arabic = request.lang === "ar" || (request.lang === "auto" && /[؀-ۿ]/.test(request.q));
  return {
    // Deliberately not sorted by score: the gateway must keep the service's order.
    items: CATALOG.map((p) => ({
      sku: p.sku,
      title: arabic ? p.ar : p.en,
      description: null,
      brand: p.sku === "B1" ? "sony" : "other",
      price: { amount: 199.5, currency: "AED" },
      category_path: "electronics.audio.headphones",
      colour: "black",
      size: null,
      score: p.score,
      rank_source: p.rank,
    })),
    facets: [{ name: "brand", buckets: [{ value: "sony", count: 1 }] }],
    applied_filters: { brands: ["sony"], colours: ["black"], price_max: 300 },
    parsed_query: {
      original: request.q,
      normalised: request.q.toLowerCase(),
      detected_lang: arabic ? "ar" : "en",
      script: arabic ? "ar" : "en",
      rewritten: "headphones",
      filters: { colours: ["black"], price_max: 300 },
      parser: "rule",
      unparsed_tokens: [],
    },
    degraded: [],
    next_cursor: NEXT_CURSOR,
    total_estimate: 3,
    timings_ms: { total: 9.5 },
    ...overrides,
  };
}

function mockSearch(overrides: Partial<InternalSearchResponse> = {}) {
  const search = jest.fn(async (request: InternalSearchRequest, _options?: SearchCallOptions) =>
    respond(request, overrides),
  );
  const client: SearchClient = { search };
  return { client, search };
}

type SearchResult = ExecutionResult<{
  search: {
    totalEstimate: number;
    degraded: string[];
    edges: Array<{ cursor: string | null; node: Record<string, unknown> & { sku: string; title: string } }>;
    pageInfo: { hasNextPage: boolean; hasPreviousPage: boolean; startCursor: string | null; endCursor: string | null };
    facets: Array<{ name: string; buckets: Array<{ value: string; count: number }> }>;
    appliedFilters: Record<string, unknown>;
    interpretation: Record<string, unknown> & { filterSources: Array<{ field: string; value: string; source: string }> };
  };
}>;

async function run(client: SearchClient, variables: Record<string, unknown>): Promise<SearchResult> {
  const contextValue: GraphQLContext = { searchClient: client, pageTokenSecret: SECRET, budgets: BUDGETS, cache: null };
  return (await graphql({ schema, source: SEARCH, variableValues: variables, contextValue })) as SearchResult;
}

function data(result: SearchResult) {
  expect(result.errors).toBeUndefined();
  const search = result.data?.search;
  if (!search) throw new Error("no data.search");
  return search;
}

describe("connection shape", () => {
  it("maps the internal response onto ProductSearchConnection", async () => {
    const { client } = mockSearch();
    const s = data(await run(client, { input: { q: "black headphones under 300", lang: "EN" }, first: 3 }));

    expect(s.totalEstimate).toBe(3);
    expect(s.edges).toHaveLength(3);
    expect(s.edges[0]?.node).toEqual({
      sku: "B1",
      title: "Sony wireless headphones",
      description: null,
      brand: "sony",
      price: { amount: 199.5, currency: "AED" },
      categoryPath: "electronics.audio.headphones",
      colour: "black",
      size: null,
      score: 0.42,
      rankSource: "RERANK",
    });
    expect(s.edges[2]?.node.rankSource).toBe("FUSED");
    expect(s.pageInfo.hasNextPage).toBe(true);
    expect(s.pageInfo.hasPreviousPage).toBe(false);
    expect(s.pageInfo.startCursor).toBeNull();
    expect(s.facets).toEqual([{ name: "brand", buckets: [{ value: "sony", count: 1 }] }]);
    expect(s.appliedFilters).toEqual({
      priceMin: null,
      priceMax: 300,
      currency: null,
      brands: ["sony"],
      colours: ["black"],
      sizes: [],
      category: null,
    });
    expect(s.interpretation).toMatchObject({
      original: "black headphones under 300",
      rewritten: "headphones",
      detectedLang: "en",
      script: "EN",
      parser: "RULE",
      unparsedTokens: [],
    });
  });

  it("tags each applied filter with where it came from", async () => {
    const { client } = mockSearch();
    const s = data(
      await run(client, { input: { q: "black headphones under 300", lang: "EN", filters: { brands: ["sony"] } } }),
    );
    expect(s.interpretation.filterSources).toEqual(
      expect.arrayContaining([
        { field: "BRAND", value: "sony", source: "EXPLICIT" },
        { field: "COLOUR", value: "black", source: "RULE" },
        { field: "PRICE_MAX", value: "300", source: "RULE" },
      ]),
    );
  });

  it("forwards limit and explicit filters to the search service in its own field names", async () => {
    const { client, search } = mockSearch();
    await run(client, {
      input: { q: "shoes", lang: "EN", filters: { priceMin: 100, priceMax: 200, brands: ["nike"], sizes: ["42"] } },
      first: 10,
    });
    expect(search).toHaveBeenCalledTimes(1);
    const request = search.mock.calls[0]?.[0];
    expect(request).toMatchObject({
      q: "shoes",
      lang: "en",
      limit: 10,
      filters: { price_min: 100, price_max: 200, brands: ["nike"], sizes: ["42"] },
    });
    expect(request?.cursor).toBeUndefined();
  });
});

describe("cursors are page tokens", () => {
  const input = { q: "headphones", lang: "EN", filters: { brands: ["sony"] } };

  it("every edge carries the page's end cursor, a signed token wrapping next_cursor", async () => {
    const { client } = mockSearch();
    const s = data(await run(client, { input, first: 3 }));
    const end = s.pageInfo.endCursor;
    expect(typeof end).toBe("string");
    expect(s.edges.every((e) => e.cursor === end)).toBe(true);
    // Not the raw cursor: the PIT id must not be visible or forgeable by clients.
    expect(end).not.toContain("pit-abc");
    const cursor = decodePageToken(end ?? "", { q: "headphones", lang: "en", filters: { brands: ["sony"] } }, { secret: SECRET });
    expect(cursor).toEqual(NEXT_CURSOR);
  });

  it("passing endCursor as `after` forwards the opaque cursor unchanged", async () => {
    const { client, search } = mockSearch();
    const first = data(await run(client, { input, first: 3 }));
    data(await run(client, { input, first: 3, after: first.pageInfo.endCursor }));
    expect(search).toHaveBeenCalledTimes(2);
    expect(search.mock.calls[1]?.[0].cursor).toEqual(NEXT_CURSOR);
  });

  it("the last page has no cursor", async () => {
    const { client } = mockSearch({ next_cursor: null });
    const s = data(await run(client, { input, first: 3 }));
    expect(s.pageInfo).toMatchObject({ hasNextPage: false, endCursor: null });
    expect(s.edges.every((e) => e.cursor === null)).toBe(true);
  });

  it("rejects a tampered `after` as INVALID_PAGE_TOKEN without calling search", async () => {
    const { client, search } = mockSearch();
    const first = data(await run(client, { input, first: 3 }));
    const token = first.pageInfo.endCursor ?? "";
    const tampered = `${token.slice(0, 5)}${token[5] === "A" ? "B" : "A"}${token.slice(6)}`;
    search.mockClear();

    const result = await run(client, { input, first: 3, after: tampered });
    expect(result.data?.search ?? null).toBeNull();
    expect(result.errors?.[0]?.extensions).toMatchObject({
      code: "INVALID_PAGE_TOKEN",
      type: expect.stringMatching(/\/invalid-page-token$/),
    });
    expect(search).not.toHaveBeenCalled();
  });

  it("rejects an `after` issued for a different query", async () => {
    const { client, search } = mockSearch();
    const first = data(await run(client, { input, first: 3 }));
    search.mockClear();
    const result = await run(client, { input: { ...input, q: "speakers" }, first: 3, after: first.pageInfo.endCursor });
    expect(result.errors?.[0]?.extensions).toMatchObject({ code: "INVALID_PAGE_TOKEN" });
    expect(search).not.toHaveBeenCalled();
  });

  it("rejects an expired `after`", async () => {
    const { client, search } = mockSearch();
    const expired = encodePageToken(NEXT_CURSOR, { q: "headphones", lang: "en", filters: { brands: ["sony"] } }, {
      secret: SECRET,
      ttlSeconds: 60,
      now: Date.now() - 3_600_000,
    });
    const result = await run(client, { input, first: 3, after: expired });
    expect(result.errors?.[0]?.extensions).toMatchObject({ code: "INVALID_PAGE_TOKEN" });
    expect(search).not.toHaveBeenCalled();
  });
});

describe("localized titles", () => {
  it.each([
    ["AR", "ar", "سماعات سوني لاسلكية"],
    ["EN", "en", "Sony wireless headphones"],
  ])("lang %s reaches the service as %s and the title comes back localized", async (lang, wire, title) => {
    const { client, search } = mockSearch();
    const s = data(await run(client, { input: { q: "headphones", lang } }));
    expect(search.mock.calls[0]?.[0].lang).toBe(wire);
    expect(s.edges[0]?.node.title).toBe(title);
  });

  it("AUTO is forwarded as auto (the service detects the language)", async () => {
    const { client, search } = mockSearch();
    const s = data(await run(client, { input: { q: "سماعات" } }));
    expect(search.mock.calls[0]?.[0].lang).toBe("auto");
    expect(s.edges[0]?.node.title).toBe("سماعات سوني لاسلكية");
    expect(s.interpretation).toMatchObject({ detectedLang: "ar", script: "AR" });
  });
});

describe("pass-through (no re-ranking, degrade don't fail)", () => {
  it("keeps the service's item order even when scores are not monotonic", async () => {
    const { client } = mockSearch();
    const s = data(await run(client, { input: { q: "headphones", lang: "EN" } }));
    expect(s.edges.map((e) => e.node.sku)).toEqual(["B1", "B2", "B3"]);
  });

  it.each<[InternalSearchResponse["degraded"], string[]]>([
    [["rerank"], ["RERANK"]],
    [["llm_parse"], ["LLM_PARSE"]],
    [["rerank", "llm_parse", "knn", "bm25", "semantic_cache"], ["RERANK", "LLM_PARSE", "KNN", "BM25", "SEMANTIC_CACHE"]],
    [[], []],
  ])("degraded %j is passed through as %j with results intact", async (degraded, expected) => {
    const { client } = mockSearch({
      degraded,
      items: respond({ q: "x", lang: "en", limit: 24 }).items.map((i) => ({ ...i, rank_source: "fused" as const })),
    });
    const s = data(await run(client, { input: { q: "headphones", lang: "EN" } }));
    expect(s.degraded).toEqual(expected);
    expect(s.edges).toHaveLength(3);
    expect(s.edges.every((e) => e.node.rankSource === "FUSED")).toBe(true);
  });
});

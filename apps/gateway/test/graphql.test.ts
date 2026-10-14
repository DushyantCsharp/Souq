import { afterEach, describe, expect, it } from "@jest/globals";
import type { FastifyInstance } from "fastify";

import { UpstreamError } from "../src/errors/upstream.js";
import { CURSOR, fakeSearchClient, searchResponse, testApp } from "./fixtures.js";

let app: FastifyInstance | undefined;
afterEach(async () => {
  await app?.close();
  app = undefined;
});

const SEARCH = /* GraphQL */ `
  query Search($input: SearchInput!, $first: Int, $after: String) {
    search(input: $input, first: $first, after: $after) {
      totalEstimate
      degraded
      edges { cursor node { sku title description price { amount currency } rankSource } }
      pageInfo { hasNextPage hasPreviousPage endCursor }
      facets { name buckets { value count } }
      appliedFilters { priceMax brands colours }
      interpretation {
        original rewritten detectedLang script parser unparsedTokens
        filters { priceMax colours brands }
        filterSources { field value source }
      }
    }
  }
`;

interface GqlResult {
  data?: { search: Record<string, unknown> & { pageInfo: { endCursor: string | null } } } | null;
  errors?: { message: string; extensions?: Record<string, unknown> }[];
}

async function gql(instance: FastifyInstance, variables: Record<string, unknown>): Promise<GqlResult> {
  const res = await instance.inject({ method: "POST", url: "/graphql", payload: { query: SEARCH, variables } });
  return JSON.parse(res.body) as GqlResult;
}

describe("POST /graphql", () => {
  it("serves search as a Relay connection with an explained interpretation", async () => {
    const client = fakeSearchClient(async () => searchResponse({ degraded: ["llm_parse"] }));
    app = await testApp({ searchClient: client });

    const result = await gql(app, {
      input: { q: "red nike shoes under 300", lang: "EN", filters: { brands: ["Nike"] } },
      first: 2,
    });

    expect(result.errors).toBeUndefined();
    const search = result.data?.search;
    expect(client.calls[0]?.request).toEqual({
      q: "red nike shoes under 300",
      lang: "en",
      limit: 2,
      filters: { brands: ["Nike"] },
    });
    expect(search?.degraded).toEqual(["LLM_PARSE"]);
    expect(search?.edges).toEqual([
      expect.objectContaining({ node: expect.objectContaining({ sku: "B000123", rankSource: "RERANK" }) }),
      expect.objectContaining({ node: expect.objectContaining({ sku: "B000456", rankSource: "FUSED" }) }),
    ]);
    const endCursor = search?.pageInfo.endCursor;
    expect(typeof endCursor).toBe("string");
    expect((search?.edges as { cursor: string }[]).every((e) => e.cursor === endCursor)).toBe(true);
    expect(search?.interpretation).toEqual(
      expect.objectContaining({
        parser: "RULE",
        script: "EN",
        rewritten: "nike shoes",
        filters: { priceMax: 300, colours: ["red"], brands: [] },
        filterSources: [
          { field: "PRICE_MAX", value: "300", source: "RULE" },
          { field: "BRAND", value: "Nike", source: "EXPLICIT" },
          { field: "COLOUR", value: "red", source: "RULE" },
        ],
      }),
    );

    // The cursor round-trips: second page sends the decoded cursor upstream.
    const page2 = await gql(app, {
      input: { q: "red nike shoes under 300", lang: "EN", filters: { brands: ["Nike"] } },
      first: 2,
      after: endCursor,
    });
    expect(page2.errors).toBeUndefined();
    expect(client.calls[1]?.request.cursor).toEqual(CURSOR);
  });

  it("reports a cursor from another query as INVALID_PAGE_TOKEN", async () => {
    app = await testApp();
    const first = await gql(app, { input: { q: "shoes" } });
    const result = await gql(app, { input: { q: "boots" }, after: first.data?.search.pageInfo.endCursor });
    expect(result.errors?.[0]?.extensions).toEqual(
      expect.objectContaining({ code: "INVALID_PAGE_TOKEN", type: "https://souq.dev/problems/invalid-page-token" }),
    );
  });

  it("rejects bad arguments as BAD_USER_INPUT without calling search", async () => {
    const client = fakeSearchClient();
    app = await testApp({ searchClient: client });
    const result = await gql(app, { input: { q: "shoes", filters: { priceMin: -5 } }, first: 500 });
    expect(result.errors?.[0]?.extensions?.code).toBe("BAD_USER_INPUT");
    const names = (result.errors?.[0]?.extensions?.errors as { name: string }[]).map((e) => e.name);
    expect(names).toEqual(expect.arrayContaining(["first", "input.filters.priceMin"]));
    expect(client.calls).toHaveLength(0);
  });

  it("maps upstream timeouts to UPSTREAM_TIMEOUT and masks unexpected errors", async () => {
    app = await testApp({
      searchClient: fakeSearchClient(() => Promise.reject(new UpstreamError("search", "timeout", "too slow"))),
    });
    const timeout = await gql(app, { input: { q: "shoes" } });
    expect(timeout.errors?.[0]?.extensions?.code).toBe("UPSTREAM_TIMEOUT");
    await app.close();

    app = await testApp({ searchClient: fakeSearchClient(() => Promise.reject(new Error("db password is hunter2"))) });
    const masked = await gql(app, { input: { q: "shoes" } });
    expect(masked.errors?.[0]?.message).toBe("Unexpected error.");
    expect(JSON.stringify(masked)).not.toContain("hunter2");
  });
});

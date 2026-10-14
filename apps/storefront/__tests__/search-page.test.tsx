/**
 * Storefront search page, rendered the way Next renders it on the server (async component → JSX), against a
 * stubbed gateway. Fixtures use the values the GraphQL schema actually returns (enums are UPPER_CASE), so a
 * mismatch between packages/contracts/schema.graphql and the UI shows up here rather than in production.
 */
import { afterEach, beforeEach, describe, expect, it, jest } from "@jest/globals";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement, ReactNode } from "react";
import LocaleLayout from "@/app/[locale]/layout";
import SearchPage from "@/app/[locale]/page";
import type { StorefrontSearchQuery, StorefrontSearchQueryVariables } from "@/lib/operations";
import { parseSearchState, type RawSearchParams } from "@/lib/search-state";

type SearchData = StorefrontSearchQuery["search"];
type FiltersData = SearchData["appliedFilters"];

function filters(overrides: Partial<FiltersData> = {}): FiltersData {
  return {
    priceMin: null,
    priceMax: null,
    currency: null,
    brands: [],
    colours: [],
    sizes: [],
    category: null,
    ...overrides,
  };
}

function searchData(overrides: Partial<SearchData> = {}): SearchData {
  return {
    totalEstimate: 2,
    degraded: [],
    edges: [
      {
        cursor: "tok-page-1",
        node: {
          sku: "B000001",
          title: "سماعات سوني لاسلكية",
          description: "سماعات رأس بلوتوث",
          brand: "sony",
          price: { amount: 499.99, currency: "AED" },
          categoryPath: "Electronics > Audio > Headphones",
          colour: "black",
          size: null,
          rankSource: "RERANK",
        },
      },
      {
        cursor: "tok-page-1",
        node: {
          sku: "B000002",
          title: "سماعات بوز",
          description: null,
          brand: "bose",
          price: { amount: 899, currency: "AED" },
          categoryPath: "Electronics > Audio > Headphones",
          colour: "white",
          size: null,
          rankSource: "FUSED",
        },
      },
    ],
    pageInfo: { hasNextPage: true, endCursor: "tok-page-1" },
    facets: [
      {
        name: "brand",
        buckets: [
          { value: "sony", count: 1 },
          { value: "bose", count: 1 },
        ],
      },
      { name: "colour", buckets: [{ value: "black", count: 1 }] },
    ],
    appliedFilters: filters(),
    interpretation: {
      original: "سماعات",
      normalised: "سماعات",
      detectedLang: "ar",
      script: "AR",
      rewritten: null,
      parser: "RULE",
      unparsedTokens: [],
      filters: filters(),
    },
    ...overrides,
  };
}

interface Call {
  url: string;
  variables: StorefrontSearchQueryVariables;
}

let calls: Call[];
let nextResponse: SearchData;
const realFetch = globalThis.fetch;

beforeEach(() => {
  calls = [];
  nextResponse = searchData();
  process.env.GATEWAY_URL = "http://gateway.test/graphql";
  // The page fetches on the server via the global fetch; the stub records the GraphQL variables it sent.
  globalThis.fetch = jest.fn(async (input: unknown, init?: { body?: unknown }) => {
    const body = JSON.parse(String(init?.body)) as { variables: StorefrontSearchQueryVariables };
    calls.push({ url: String(input), variables: body.variables });
    return { status: 200, json: async () => ({ data: { search: nextResponse } }) };
  }) as unknown as typeof fetch;
});

afterEach(() => {
  cleanup();
  globalThis.fetch = realFetch;
  delete process.env.GATEWAY_URL;
});

async function renderPage(locale: string, searchParams: RawSearchParams) {
  const ui = await SearchPage({ params: Promise.resolve({ locale }), searchParams: Promise.resolve(searchParams) });
  return render(ui);
}

/** `href` → the searchParams object Next would hand the page after navigating there. */
function paramsFromHref(href: string): RawSearchParams {
  const url = new URL(href, "http://storefront.test");
  const out: RawSearchParams = {};
  for (const key of new Set(url.searchParams.keys())) {
    const values = url.searchParams.getAll(key);
    out[key] = values.length === 1 ? values[0] : values;
  }
  return out;
}

describe("locale layout", () => {
  it("renders <html dir=rtl lang=ar> for Arabic and ltr for English", async () => {
    const children: ReactNode = <p>child</p>;
    const ar = (await LocaleLayout({ children, params: Promise.resolve({ locale: "ar" }) })) as ReactElement<{
      dir: string;
      lang: string;
    }>;
    expect(ar.type).toBe("html");
    expect(ar.props).toMatchObject({ dir: "rtl", lang: "ar" });

    const en = (await LocaleLayout({ children, params: Promise.resolve({ locale: "en" }) })) as ReactElement<{
      dir: string;
      lang: string;
    }>;
    expect(en.props).toMatchObject({ dir: "ltr", lang: "en" });
  });
});

describe("search results page", () => {
  it("renders Arabic results and asks the gateway for lang AR", async () => {
    await renderPage("ar", { q: "سماعات" });

    expect(calls).toHaveLength(1);
    expect(calls[0]?.url).toBe("http://gateway.test/graphql");
    expect(calls[0]?.variables.input).toMatchObject({ q: "سماعات", lang: "AR" });
    expect(calls[0]?.variables.after).toBeUndefined();

    const results = screen.getByRole("main");
    const cards = within(results).getAllByRole("article");
    expect(cards.map((c) => c.getAttribute("data-sku"))).toEqual(["B000001", "B000002"]);
    const title = within(cards[0]!).getByRole("heading", { name: "سماعات سوني لاسلكية" });
    // Titles can fall back to the other language, so they must pick their own direction.
    expect(title).toHaveAttribute("dir", "auto");
    // The search box accepts either script.
    expect(screen.getByRole("searchbox")).toHaveAttribute("dir", "auto");
  });

  it("keeps the gateway's order (the gateway and UI never re-rank)", async () => {
    const data = searchData();
    nextResponse = { ...data, edges: [...data.edges].reverse() };
    await renderPage("en", { q: "headphones" });
    const skus = screen.getAllByRole("article").map((c) => c.getAttribute("data-sku"));
    expect(skus).toEqual(["B000002", "B000001"]);
  });

  it("asks for lang EN on the English UI", async () => {
    await renderPage("en", { q: "headphones" });
    expect(calls[0]?.variables.input).toMatchObject({ q: "headphones", lang: "EN" });
  });

  it("does not call the gateway without a query", async () => {
    await renderPage("en", {});
    expect(calls).toHaveLength(0);
  });
});

describe("facets", () => {
  it("clicking a facet value navigates to the same query with that filter and page 1", async () => {
    const user = userEvent.setup();
    // Start on page 2 so the test also proves the facet link drops the stale page token.
    await renderPage("en", { q: "headphones", after: "tok-page-1" });
    expect(calls[0]?.variables.after).toBe("tok-page-1");

    const navigations: string[] = [];
    const capture = (e: MouseEvent) => {
      const a = (e.target as Element | null)?.closest("a");
      if (a) {
        navigations.push(a.getAttribute("href") ?? "");
        e.preventDefault();
      }
    };
    document.addEventListener("click", capture);
    try {
      const facets = screen.getByRole("complementary");
      await user.click(within(facets).getByRole("link", { name: /sony/i }));
    } finally {
      document.removeEventListener("click", capture);
    }

    expect(navigations).toHaveLength(1);
    const target = paramsFromHref(navigations[0]!);
    expect(target).toMatchObject({ q: "headphones", brand: "sony" });
    expect(target.after).toBeUndefined();
    expect(navigations[0]!.startsWith("/en?")).toBe(true);

    // Follow the navigation: the next server render sends the filter and marks the bucket selected.
    cleanup();
    calls = [];
    nextResponse = searchData({ appliedFilters: filters({ brands: ["sony"] }) });
    await renderPage("en", target);
    expect(calls[0]?.variables.input.filters).toEqual({ brands: ["sony"] });
    expect(calls[0]?.variables.after).toBeUndefined();
    const sony = within(screen.getByRole("complementary")).getByRole("link", { name: /sony/i });
    expect(sony).toHaveAttribute("data-selected", "true");
    expect(parseSearchState(paramsFromHref(sony.getAttribute("href") ?? "")).filters.brands).toEqual([]);
  });

  it("toggling a second value keeps the first (OR within a field)", async () => {
    await renderPage("en", { q: "headphones", brand: "sony" });
    const bose = within(screen.getByRole("complementary")).getByRole("link", { name: /bose/i });
    const target = parseSearchState(paramsFromHref(bose.getAttribute("href") ?? ""));
    expect(target.filters.brands).toEqual(["sony", "bose"]);
    expect(target.q).toBe("headphones");
  });
});

describe("degraded notice", () => {
  it("is absent when the response is fully healthy", async () => {
    await renderPage("en", { q: "headphones" });
    expect(screen.queryByTestId("degraded-notice")).not.toBeInTheDocument();
  });

  it("explains a skipped reranker using the schema's Degradation enum value", async () => {
    nextResponse = searchData({ degraded: ["RERANK"] });
    await renderPage("en", { q: "headphones" });
    const notice = screen.getByTestId("degraded-notice");
    expect(notice).toHaveAttribute("role", "status");
    expect(notice).toHaveTextContent("Re-ranking was skipped");
    expect(notice).not.toHaveTextContent("Part of the search pipeline was unavailable");
  });

  it("explains an LLM parser fallback in Arabic", async () => {
    nextResponse = searchData({ degraded: ["LLM_PARSE"] });
    await renderPage("ar", { q: "سماعات" });
    expect(screen.getByTestId("degraded-notice")).toHaveTextContent("محلل الاستعلامات الذكي غير متاح");
  });

  it("still renders the results alongside the notice", async () => {
    nextResponse = searchData({ degraded: ["RERANK", "KNN"] });
    await renderPage("en", { q: "headphones" });
    expect(screen.getAllByRole("article")).toHaveLength(2);
    expect(within(screen.getByTestId("degraded-notice")).getAllByRole("listitem")).toHaveLength(2);
  });
});

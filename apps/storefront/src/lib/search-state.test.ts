/** @jest-environment node */
import { describe, expect, it } from "@jest/globals";
import {
  clearFilters,
  hrefFor,
  nextPage,
  pageNumber,
  parseSearchState,
  previousPage,
  removePrice,
  toggleFilter,
} from "./search-state";

describe("parseSearchState", () => {
  it("reads repeated filters, trims and de-duplicates them", () => {
    const s = parseSearchState({ q: "  headphones ", brand: ["Sony", " Sony", "Bose", ""], colour: "black" });
    expect(s.q).toBe("headphones");
    expect(s.filters.brands).toEqual(["Sony", "Bose"]);
    expect(s.filters.colours).toEqual(["black"]);
    expect(s.filters.sizes).toEqual([]);
  });

  it("drops invalid prices and currencies and swaps an inverted range", () => {
    expect(parseSearchState({ q: "x", price_min: "abc", price_max: "-5" }).filters).toMatchObject({
      priceMin: undefined,
      priceMax: undefined,
    });
    expect(parseSearchState({ q: "x", price_min: "300", price_max: "100" }).filters).toMatchObject({
      priceMin: 100,
      priceMax: 300,
    });
    expect(parseSearchState({ q: "x", currency: "aed" }).filters.currency).toBe("AED");
    expect(parseSearchState({ q: "x", currency: "dollars" }).filters.currency).toBeUndefined();
  });

  it("ignores a cursor history without a current cursor", () => {
    expect(parseSearchState({ q: "x", prev: ["t1"] }).prev).toEqual([]);
  });
});

describe("paging through forward-only cursors", () => {
  it("walks next/next/prev/prev back to page 1", () => {
    const p1 = parseSearchState({ q: "tv" });
    expect(pageNumber(p1)).toBe(1);
    expect(previousPage(p1)).toBeNull();

    const p2 = nextPage(p1, "c1");
    expect(p2).toMatchObject({ after: "c1", prev: [] });
    expect(pageNumber(p2)).toBe(2);

    const p3 = nextPage(p2, "c2");
    expect(p3).toMatchObject({ after: "c2", prev: ["c1"] });
    expect(pageNumber(p3)).toBe(3);

    const back2 = previousPage(p3);
    expect(back2).toMatchObject({ after: "c1", prev: [] });
    const back1 = back2 && previousPage(back2);
    expect(back1).toMatchObject({ after: undefined, prev: [] });
  });

  it("round-trips through the URL", () => {
    const p3 = nextPage(nextPage(parseSearchState({ q: "tv", brand: "LG" }), "c1"), "c2");
    const href = hrefFor("ar", p3);
    expect(href.startsWith("/ar?")).toBe(true);
    const sp = new URLSearchParams(href.split("?")[1]);
    const reparsed = parseSearchState({ q: sp.get("q") ?? undefined, brand: sp.getAll("brand"), after: sp.get("after") ?? undefined, prev: sp.getAll("prev") });
    expect(reparsed).toEqual(p3);
  });
});

describe("filter transitions reset paging", () => {
  const onPage3 = nextPage(nextPage(parseSearchState({ q: "shoes", size: "42", price_max: "200" }), "c1"), "c2");

  it("toggles list filters on and off", () => {
    const added = toggleFilter(onPage3, "brand", "Nike");
    expect(added.filters.brands).toEqual(["Nike"]);
    expect(added).toMatchObject({ after: undefined, prev: [] });
    expect(toggleFilter(added, "brand", "Nike").filters.brands).toEqual([]);
  });

  it("treats category as single-valued", () => {
    const a = toggleFilter(onPage3, "category", "Shoes");
    expect(toggleFilter(a, "category", "Boots").filters.category).toBe("Boots");
    expect(toggleFilter(a, "category", "Shoes").filters.category).toBeUndefined();
  });

  it("removes price bounds and clears everything", () => {
    expect(removePrice(onPage3, "priceMax").filters.priceMax).toBeUndefined();
    const cleared = clearFilters(onPage3);
    expect(cleared.filters).toEqual({ brands: [], colours: [], sizes: [] });
    expect(cleared.q).toBe("shoes");
    expect(hrefFor("en", cleared)).toBe("/en?q=shoes");
  });
});

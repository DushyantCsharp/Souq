// Response-cache keys must be equal exactly when the cached body would be equal: cosmetic differences
// (filter key/array order, duplicate values, surrounding whitespace) must not split the cache, and anything
// that changes the body (lang, limit, page token, filter values, the query text itself) must not collide.
import { describe, expect, it } from "@jest/globals";

import { type SearchCacheKeyInput, searchCacheKey } from "../src/cache/keys.js";

const base: SearchCacheKeyInput = {
  q: "sony headphones",
  lang: "en",
  filters: { brands: ["sony", "bose"], colours: ["black"], price_max: 300 },
  limit: 24,
  page_token: null,
};

const key = (overrides: Partial<SearchCacheKeyInput> = {}): string => searchCacheKey({ ...base, ...overrides });

describe("searchCacheKey shape", () => {
  it("is a namespaced sha256", () => {
    expect(key()).toMatch(/^souq:gw:search:v1:[0-9a-f]{64}$/);
  });

  it("never contains the raw query text (keys show up in Redis tooling and logs)", () => {
    expect(key({ q: "very-private-query" })).not.toContain("very-private-query");
    expect(key({ q: "سماعات" })).not.toContain("سماعات");
  });

  it("is deterministic", () => {
    expect(key()).toBe(key());
  });
});

describe("searchCacheKey is stable under cosmetic differences", () => {
  it("filter key order", () => {
    expect(key({ filters: { price_max: 300, colours: ["black"], brands: ["sony", "bose"] } })).toBe(key());
  });

  it("order of values inside a multi-valued filter", () => {
    expect(key({ filters: { brands: ["bose", "sony"], colours: ["black"], price_max: 300 } })).toBe(key());
  });

  it("duplicate values inside a multi-valued filter", () => {
    expect(key({ filters: { brands: ["sony", "bose", "sony"], colours: ["black", "black"], price_max: 300 } })).toBe(
      key(),
    );
  });

  it("absent, null, empty and empty-array filters", () => {
    const none = key({ filters: undefined });
    expect(key({ filters: null })).toBe(none);
    expect(key({ filters: {} })).toBe(none);
    expect(key({ filters: { brands: [], colours: [], category: null, price_min: undefined } })).toBe(none);
  });

  it.each(["  sony headphones", "sony headphones  ", "sony   headphones", "\tsony\nheadphones "])(
    "surrounding/internal whitespace in q: %j",
    (q) => {
      expect(key({ q })).toBe(key());
    },
  );

  it("lang defaults to auto and is case-insensitive", () => {
    expect(key({ lang: undefined })).toBe(key({ lang: "auto" }));
    expect(key({ lang: null })).toBe(key({ lang: "auto" }));
    expect(key({ lang: "EN" })).toBe(key({ lang: "en" }));
  });

  it("limit defaults to the contract default (24)", () => {
    expect(key({ limit: undefined })).toBe(key({ limit: 24 }));
    expect(key({ limit: null })).toBe(key({ limit: 24 }));
  });

  it("page_token and its camelCase alias are the same input", () => {
    expect(key({ page_token: undefined, pageToken: "tok-1" })).toBe(key({ page_token: "tok-1" }));
    expect(key({ page_token: undefined })).toBe(key({ page_token: null }));
  });
});

describe("searchCacheKey distinguishes anything that changes the response", () => {
  it.each<[string, Partial<SearchCacheKeyInput>]>([
    ["lang ar", { lang: "ar" }],
    ["lang auto", { lang: "auto" }],
    ["limit 25", { limit: 25 }],
    ["limit 1", { limit: 1 }],
    ["a page token", { page_token: "tok-page-2" }],
    ["another brand", { filters: { brands: ["sony"], colours: ["black"], price_max: 300 } }],
    ["another colour", { filters: { brands: ["sony", "bose"], colours: ["white"], price_max: 300 } }],
    ["another price", { filters: { brands: ["sony", "bose"], colours: ["black"], price_max: 301 } }],
    ["price_min instead of price_max", { filters: { brands: ["sony", "bose"], colours: ["black"], price_min: 300 } }],
    ["an added category", { filters: { ...base.filters, category: "electronics.audio" } }],
    ["no filters", { filters: undefined }],
    ["another query", { q: "bose headphones" }],
  ])("%s", (_name, overrides) => {
    expect(key(overrides)).not.toBe(key());
  });

  it("distinct page tokens never share a key", () => {
    const keys = new Set(["t1", "t2", "t3", "t1 "].map((t) => key({ page_token: t })));
    expect(keys.size).toBe(4);
  });

  it("treats q case as significant (parsed_query.original echoes the raw text)", () => {
    expect(key({ q: "Sony Headphones" })).not.toBe(key({ q: "sony headphones" }));
  });

  it("does not apply Arabic normalisation (that lives only in services/search)", () => {
    // Different raw text → different parsed_query.original in the body, so a shared key would be wrong.
    expect(key({ q: "سمّاعات", lang: "ar" })).not.toBe(key({ q: "سماعات", lang: "ar" }));
  });

  it("separates a value moved between fields", () => {
    const a = key({ filters: { brands: ["black"] } });
    const b = key({ filters: { colours: ["black"] } });
    expect(a).not.toBe(b);
  });

  it("separates multi-valued filters from their concatenation", () => {
    const a = key({ filters: { brands: ["a,b"] } });
    const b = key({ filters: { brands: ["a", "b"] } });
    expect(a).not.toBe(b);
  });
});

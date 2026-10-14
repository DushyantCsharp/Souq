// All search UI state lives in the URL, so every view (including page N of a filtered search) is a plain
// link: shareable, back-button friendly and rendered entirely on the server without client JavaScript.
import { z } from "zod";
import type { Locale } from "@/i18n/config";
import type { FacetKind } from "@/i18n/messages";

export interface FilterState {
  brands: string[];
  colours: string[];
  sizes: string[];
  category?: string | undefined;
  priceMin?: number | undefined;
  priceMax?: number | undefined;
  currency?: string | undefined;
}

export interface SearchState {
  q: string;
  filters: FilterState;
  /** Opaque gateway page token for the current page; absent on page 1. */
  after?: string | undefined;
  /**
   * Tokens of the pages before the current one (page 1 has no token, so it is never stored). The gateway's
   * cursors are forward-only (they wrap an OpenSearch PIT + search_after), so "previous" replays a stored
   * token instead of asking the API to page backwards.
   */
  prev: string[];
}

export type RawSearchParams = Record<string, string | string[] | undefined>;

/** Query-string names. Multi-valued filters repeat the key (`?brand=Sony&brand=Bose`). */
export const PARAM = {
  q: "q",
  brand: "brand",
  colour: "colour",
  size: "size",
  category: "category",
  priceMin: "price_min",
  priceMax: "price_max",
  currency: "currency",
  after: "after",
  prev: "prev",
} as const;

const MAX_QUERY_LENGTH = 256;
const MAX_VALUES_PER_FILTER = 20;
// Bounds the URL: tokens are a few hundred bytes each, and deep paging is not a real shopper behaviour.
const MAX_PREV_TOKENS = 20;

const many = z
  .union([z.string(), z.array(z.string())])
  .optional()
  .transform((v) => {
    const values = v === undefined ? [] : Array.isArray(v) ? v : [v];
    const cleaned = values.map((s) => s.trim()).filter((s) => s.length > 0);
    return [...new Set(cleaned)].slice(0, MAX_VALUES_PER_FILTER);
  });

const one = z
  .union([z.string(), z.array(z.string())])
  .optional()
  .transform((v) => {
    const value = (Array.isArray(v) ? v[0] : v)?.trim();
    return value ? value : undefined;
  });

const price = one.transform((v) => {
  if (v === undefined) return undefined;
  const n = Number(v);
  return Number.isFinite(n) && n >= 0 ? n : undefined;
});

const rawSchema = z.object({
  [PARAM.q]: one.transform((v) => (v ?? "").slice(0, MAX_QUERY_LENGTH)),
  [PARAM.brand]: many,
  [PARAM.colour]: many,
  [PARAM.size]: many,
  [PARAM.category]: one,
  [PARAM.priceMin]: price,
  [PARAM.priceMax]: price,
  [PARAM.currency]: one.transform((v) => (v && /^[A-Za-z]{3}$/.test(v) ? v.toUpperCase() : undefined)),
  [PARAM.after]: one,
  [PARAM.prev]: many.transform((v) => v.slice(-MAX_PREV_TOKENS)),
});

export function parseSearchState(params: RawSearchParams): SearchState {
  // safeParse cannot fail on this schema (every field is optional and coerced), but never throw on a URL.
  const parsed = rawSchema.safeParse(params);
  if (!parsed.success) return emptyState();
  const p = parsed.data;
  let priceMin = p[PARAM.priceMin];
  let priceMax = p[PARAM.priceMax];
  if (priceMin !== undefined && priceMax !== undefined && priceMin > priceMax) {
    [priceMin, priceMax] = [priceMax, priceMin];
  }
  const after = p[PARAM.after];
  return {
    q: p[PARAM.q],
    filters: {
      brands: p[PARAM.brand],
      colours: p[PARAM.colour],
      sizes: p[PARAM.size],
      category: p[PARAM.category],
      priceMin,
      priceMax,
      currency: p[PARAM.currency],
    },
    after,
    // A history without a current page is meaningless (and would make "previous" skip a page).
    prev: after === undefined ? [] : p[PARAM.prev],
  };
}

export function emptyState(): SearchState {
  return { q: "", filters: { brands: [], colours: [], sizes: [] }, prev: [] };
}

export function toSearchParams(state: SearchState): URLSearchParams {
  const sp = new URLSearchParams();
  if (state.q) sp.set(PARAM.q, state.q);
  for (const b of state.filters.brands) sp.append(PARAM.brand, b);
  for (const c of state.filters.colours) sp.append(PARAM.colour, c);
  for (const s of state.filters.sizes) sp.append(PARAM.size, s);
  if (state.filters.category) sp.set(PARAM.category, state.filters.category);
  if (state.filters.priceMin !== undefined) sp.set(PARAM.priceMin, String(state.filters.priceMin));
  if (state.filters.priceMax !== undefined) sp.set(PARAM.priceMax, String(state.filters.priceMax));
  if (state.filters.currency) sp.set(PARAM.currency, state.filters.currency);
  if (state.after) {
    for (const t of state.prev) sp.append(PARAM.prev, t);
    sp.set(PARAM.after, state.after);
  }
  return sp;
}

export function hrefFor(locale: Locale, state: SearchState): string {
  const qs = toSearchParams(state).toString();
  return qs ? `/${locale}?${qs}` : `/${locale}`;
}

/**
 * Any change to the query, filters or language invalidates the page tokens (the gateway binds each token to a
 * hash of q|lang|filters), so every such transition goes back to page 1.
 */
export function firstPage(state: SearchState): SearchState {
  return { ...state, after: undefined, prev: [] };
}

export function nextPage(state: SearchState, endCursor: string): SearchState {
  return { ...state, after: endCursor, prev: state.after ? [...state.prev, state.after] : [] };
}

export function previousPage(state: SearchState): SearchState | null {
  if (!state.after) return null;
  const prev = state.prev.slice(0, -1);
  return { ...state, after: state.prev.at(-1), prev };
}

export function pageNumber(state: SearchState): number {
  return state.after ? state.prev.length + 2 : 1;
}

const LIST_KEY: Record<Exclude<FacetKind, "category">, "brands" | "colours" | "sizes"> = {
  brand: "brands",
  colour: "colours",
  size: "sizes",
};

export function isFilterSelected(state: SearchState, kind: FacetKind, value: string): boolean {
  if (kind === "category") return state.filters.category === value;
  return state.filters[LIST_KEY[kind]].includes(value);
}

export function toggleFilter(state: SearchState, kind: FacetKind, value: string): SearchState {
  const filters = { ...state.filters };
  if (kind === "category") {
    filters.category = filters.category === value ? undefined : value;
  } else {
    const key = LIST_KEY[kind];
    const current = filters[key];
    filters[key] = current.includes(value) ? current.filter((v) => v !== value) : [...current, value];
  }
  return firstPage({ ...state, filters });
}

export type PriceBound = "priceMin" | "priceMax";

export function removePrice(state: SearchState, bound: PriceBound): SearchState {
  return firstPage({ ...state, filters: { ...state.filters, [bound]: undefined } });
}

export function clearFilters(state: SearchState): SearchState {
  return firstPage({ ...state, filters: { brands: [], colours: [], sizes: [] } });
}

export function hasFilters(filters: FilterState): boolean {
  return (
    filters.brands.length > 0 ||
    filters.colours.length > 0 ||
    filters.sizes.length > 0 ||
    filters.category !== undefined ||
    filters.priceMin !== undefined ||
    filters.priceMax !== undefined ||
    filters.currency !== undefined
  );
}

export function isFacetKind(name: string): name is FacetKind {
  return name === "brand" || name === "colour" || name === "size" || name === "category";
}

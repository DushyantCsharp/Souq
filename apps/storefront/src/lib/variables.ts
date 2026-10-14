import type { Locale } from "@/i18n/config";
import type { FiltersInput, Lang, StorefrontSearchQueryVariables } from "./operations";
import type { FilterState, SearchState } from "./search-state";

/** Results per page. A UI choice, not a latency budget; the search API allows 1..50. */
export const PAGE_SIZE = 24;

export function toFiltersInput(filters: FilterState): FiltersInput | undefined {
  const input: FiltersInput = {};
  if (filters.brands.length > 0) input.brands = filters.brands;
  if (filters.colours.length > 0) input.colours = filters.colours;
  if (filters.sizes.length > 0) input.sizes = filters.sizes;
  if (filters.category) input.category = filters.category;
  if (filters.priceMin !== undefined) input.priceMin = filters.priceMin;
  if (filters.priceMax !== undefined) input.priceMax = filters.priceMax;
  if (filters.currency) input.currency = filters.currency;
  return Object.keys(input).length > 0 ? input : undefined;
}

/**
 * The UI locale doubles as the search `lang`: it decides which title/description the service returns. Query
 * language detection (detected_lang/script) still happens server-side regardless of this value.
 */
export function toSearchVariables(state: SearchState, locale: Locale): StorefrontSearchQueryVariables {
  const lang: Lang = locale === "ar" ? "AR" : "EN";
  const filters = toFiltersInput(state.filters);
  return {
    input: { q: state.q, lang, ...(filters ? { filters } : {}) },
    first: PAGE_SIZE,
    ...(state.after ? { after: state.after } : {}),
  };
}

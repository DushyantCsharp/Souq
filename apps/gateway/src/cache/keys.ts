// Response-cache keys: a stable hash over everything that changes the response body.
import { canonicalFilters, canonicalJson, canonicalLang, canonicalQuery, sha256 } from "../search/canonical.js";
import { DEFAULT_LIMIT } from "../search/types.js";

export const SEARCH_CACHE_PREFIX = "souq:gw:search:v1:";

export interface SearchCacheKeyInput {
  q: string;
  lang?: string | null;
  filters?: Readonly<Record<string, unknown>> | null;
  limit?: number | null;
  page_token?: string | null;
  /** camelCase alias of `page_token`. */
  pageToken?: string | null;
}

/**
 * `souq:gw:search:v1:<sha256>` over q (whitespace-canonical), lang (default auto), filters (sorted keys,
 * sorted de-duplicated arrays, empties dropped), limit (default 24) and page token. Bump `v1` whenever
 * the cached response shape changes.
 */
export function searchCacheKey(input: SearchCacheKeyInput): string {
  const material = {
    q: canonicalQuery(input.q),
    lang: canonicalLang(input.lang),
    filters: canonicalFilters(input.filters),
    limit: input.limit ?? DEFAULT_LIMIT,
    page_token: input.page_token ?? input.pageToken ?? null,
  };
  return SEARCH_CACHE_PREFIX + sha256(canonicalJson(material));
}

// Shapes come from the generated contracts so a spec change surfaces as a type error here.
import type { components as PublicComponents } from "@souq/contracts/public-api";
import type { paths as SearchPaths } from "@souq/contracts/search-internal";

type SearchOperation = SearchPaths["/search"]["post"];

export type InternalSearchRequest = SearchOperation["requestBody"]["content"]["application/json"];
export type InternalSearchResponse = SearchOperation["responses"][200]["content"]["application/json"];
export type InternalSearchItem = InternalSearchResponse["items"][number];
export type InternalFilters = NonNullable<InternalSearchRequest["filters"]>;
/** Opaque to the gateway: produced and interpreted only by services/search. */
export type Cursor = NonNullable<InternalSearchRequest["cursor"]>;

export type PublicSchemas = PublicComponents["schemas"];
export type PublicSearchResponse = PublicSchemas["SearchResponse"];
export type PublicSearchItem = PublicSchemas["SearchItem"];
export type Filters = PublicSchemas["Filters"];
export type Lang = PublicSchemas["LangParam"];

// Contract defaults/limits from openapi.yaml (not latency budgets).
export const DEFAULT_LIMIT = 24;
export const MAX_LIMIT = 50;
export const MAX_QUERY_LENGTH = 256;
export const MAX_PAGE_TOKEN_LENGTH = 4096;
export const LANGS: readonly Lang[] = ["en", "ar", "auto"];

/** Validated, canonical search request as both REST and GraphQL hand it to executeSearch. */
export interface SearchInput {
  q: string;
  lang: Lang;
  filters: Filters;
  limit: number;
  pageToken: string | null;
}

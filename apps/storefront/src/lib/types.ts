// View-model types derived from the operation's result type, so components follow the GraphQL schema (via
// codegen) instead of restating it. NonNullable keeps them valid if codegen marks fields nullable.
import type { StorefrontSearchQuery } from "./operations";

export type SearchResult = NonNullable<StorefrontSearchQuery["search"]>;
export type ProductEdge = NonNullable<SearchResult["edges"]>[number];
export type ProductItem = NonNullable<ProductEdge["node"]>;
export type Facet = NonNullable<SearchResult["facets"]>[number];
export type FacetBucket = NonNullable<Facet["buckets"]>[number];
export type AppliedFilters = NonNullable<SearchResult["appliedFilters"]>;
export type Interpretation = NonNullable<SearchResult["interpretation"]>;
export type PageInfo = NonNullable<SearchResult["pageInfo"]>;

/* eslint-disable */
/** Internal type. DO NOT USE DIRECTLY. */
type Exact<T extends { [key: string]: unknown }> = { [K in keyof T]: T[K] };
/** Internal type. DO NOT USE DIRECTLY. */
export type Incremental<T> = T | { [P in keyof T]?: P extends ' $fragmentName' | '__typename' ? T[P] : never };
import type { DocumentTypeDecoration } from '@graphql-typed-document-node/core';
export type Degradation =
  /** Lexical retrieval unavailable; vector only */
  | 'BM25'
  /** Vector retrieval unavailable; lexical only */
  | 'KNN'
  /** LLM parser timed out or failed; rule-parser result used */
  | 'LLM_PARSE'
  /** Reranker over budget or down; fused order returned */
  | 'RERANK'
  /** Semantic parse cache unavailable */
  | 'SEMANTIC_CACHE';

/** Explicit filters. They are merged with filters the parser extracts from `q` (explicit wins). */
export type FiltersInput = {
  /** OR within a field, AND across fields */
  brands?: Array<string> | null | undefined;
  /** Category path prefix, e.g. "Electronics > Audio" */
  category?: string | null | undefined;
  colours?: Array<string> | null | undefined;
  /** ISO 4217 code, e.g. AED */
  currency?: string | null | undefined;
  priceMax?: number | null | undefined;
  priceMin?: number | null | undefined;
  sizes?: Array<string> | null | undefined;
};

export type Lang =
  | 'AR'
  | 'AUTO'
  | 'EN';

export type ParserSource =
  /** Semantic cache hit of an earlier LLM parse */
  | 'CACHE'
  /** LLM parser */
  | 'LLM'
  /** Deterministic rule parser */
  | 'RULE';

export type RankSource =
  /** Ordered by reciprocal-rank fusion of lexical and vector retrieval */
  | 'FUSED'
  /** Past the rerank window; lexical order */
  | 'LEXICAL_TAIL'
  /** Ordered by the cross-encoder reranker */
  | 'RERANK';

export type Script =
  | 'AR'
  | 'ARABIZI'
  | 'EN'
  | 'MIXED';

export type SearchInput = {
  filters?: FiltersInput | null | undefined;
  /** Localizes `Product.title`/`Product.description`; AUTO uses the detected query language. */
  lang?: Lang | null | undefined;
  /** Free text in English, Arabic, Arabizi or a mix (1..256 chars). */
  q: string;
};

export type StorefrontSearchQueryVariables = Exact<{
  input: SearchInput;
  first: number;
  after?: string | null | undefined;
}>;


export type StorefrontSearchQuery = { search: { totalEstimate: number, degraded: Array<Degradation>, edges: Array<{ cursor: string | null, node: { sku: string, title: string, description: string | null, brand: string, categoryPath: string, colour: string | null, size: string | null, rankSource: RankSource, price: { amount: number, currency: string } } }>, pageInfo: { hasNextPage: boolean, endCursor: string | null }, facets: Array<{ name: string, buckets: Array<{ value: string, count: number }> }>, appliedFilters: { priceMin: number | null, priceMax: number | null, currency: string | null, brands: Array<string>, colours: Array<string>, sizes: Array<string>, category: string | null }, interpretation: { original: string, normalised: string, detectedLang: string, script: Script, rewritten: string | null, parser: ParserSource, unparsedTokens: Array<string>, filters: { priceMin: number | null, priceMax: number | null, currency: string | null, brands: Array<string>, colours: Array<string>, sizes: Array<string>, category: string | null } } } };

export class TypedDocumentString<TResult, TVariables>
  extends String
  implements DocumentTypeDecoration<TResult, TVariables>
{
  __apiType?: NonNullable<DocumentTypeDecoration<TResult, TVariables>['__apiType']>;
  private value: string;
  public __meta__?: Record<string, any> | undefined;

  constructor(value: string, __meta__?: Record<string, any> | undefined) {
    super(value);
    this.value = value;
    this.__meta__ = __meta__;
  }

  override toString(): string & DocumentTypeDecoration<TResult, TVariables> {
    return this.value;
  }
}

export const StorefrontSearchDocument = new TypedDocumentString(`
    query StorefrontSearch($input: SearchInput!, $first: Int!, $after: String) {
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
        price {
          amount
          currency
        }
        categoryPath
        colour
        size
        rankSource
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
    facets {
      name
      buckets {
        value
        count
      }
    }
    appliedFilters {
      priceMin
      priceMax
      currency
      brands
      colours
      sizes
      category
    }
    interpretation {
      original
      normalised
      detectedLang
      script
      rewritten
      parser
      unparsedTokens
      filters {
        priceMin
        priceMax
        currency
        brands
        colours
        sizes
        category
      }
    }
  }
}
    `) as unknown as TypedDocumentString<StorefrontSearchQuery, StorefrontSearchQueryVariables>;
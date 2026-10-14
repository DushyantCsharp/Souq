/* eslint-disable */
import * as types from './graphql';



/**
 * Map of all GraphQL operations in the project.
 *
 * This map has several performance disadvantages:
 * 1. It is not tree-shakeable, so it will include all operations in the project.
 * 2. It is not minifiable, so the string of a GraphQL query will be multiple times inside the bundle.
 * 3. It does not support dead code elimination, so it will add unused operations.
 *
 * Therefore it is highly recommended to use the babel or swc plugin for production.
 * Learn more about it here: https://the-guild.dev/graphql/codegen/plugins/presets/preset-client#reducing-bundle-size
 */
type Documents = {
    "query StorefrontSearch($input: SearchInput!, $first: Int!, $after: String) {\n  search(input: $input, first: $first, after: $after) {\n    totalEstimate\n    degraded\n    edges {\n      cursor\n      node {\n        sku\n        title\n        description\n        brand\n        price {\n          amount\n          currency\n        }\n        categoryPath\n        colour\n        size\n        rankSource\n      }\n    }\n    pageInfo {\n      hasNextPage\n      endCursor\n    }\n    facets {\n      name\n      buckets {\n        value\n        count\n      }\n    }\n    appliedFilters {\n      priceMin\n      priceMax\n      currency\n      brands\n      colours\n      sizes\n      category\n    }\n    interpretation {\n      original\n      normalised\n      detectedLang\n      script\n      rewritten\n      parser\n      unparsedTokens\n      filters {\n        priceMin\n        priceMax\n        currency\n        brands\n        colours\n        sizes\n        category\n      }\n    }\n  }\n}": typeof types.StorefrontSearchDocument,
};
const documents: Documents = {
    "query StorefrontSearch($input: SearchInput!, $first: Int!, $after: String) {\n  search(input: $input, first: $first, after: $after) {\n    totalEstimate\n    degraded\n    edges {\n      cursor\n      node {\n        sku\n        title\n        description\n        brand\n        price {\n          amount\n          currency\n        }\n        categoryPath\n        colour\n        size\n        rankSource\n      }\n    }\n    pageInfo {\n      hasNextPage\n      endCursor\n    }\n    facets {\n      name\n      buckets {\n        value\n        count\n      }\n    }\n    appliedFilters {\n      priceMin\n      priceMax\n      currency\n      brands\n      colours\n      sizes\n      category\n    }\n    interpretation {\n      original\n      normalised\n      detectedLang\n      script\n      rewritten\n      parser\n      unparsedTokens\n      filters {\n        priceMin\n        priceMax\n        currency\n        brands\n        colours\n        sizes\n        category\n      }\n    }\n  }\n}": types.StorefrontSearchDocument,
};

/**
 * The graphql function is used to parse GraphQL queries into a document that can be used by GraphQL clients.
 */
export function graphql(source: "query StorefrontSearch($input: SearchInput!, $first: Int!, $after: String) {\n  search(input: $input, first: $first, after: $after) {\n    totalEstimate\n    degraded\n    edges {\n      cursor\n      node {\n        sku\n        title\n        description\n        brand\n        price {\n          amount\n          currency\n        }\n        categoryPath\n        colour\n        size\n        rankSource\n      }\n    }\n    pageInfo {\n      hasNextPage\n      endCursor\n    }\n    facets {\n      name\n      buckets {\n        value\n        count\n      }\n    }\n    appliedFilters {\n      priceMin\n      priceMax\n      currency\n      brands\n      colours\n      sizes\n      category\n    }\n    interpretation {\n      original\n      normalised\n      detectedLang\n      script\n      rewritten\n      parser\n      unparsedTokens\n      filters {\n        priceMin\n        priceMax\n        currency\n        brands\n        colours\n        sizes\n        category\n      }\n    }\n  }\n}"): typeof import('./graphql').StorefrontSearchDocument;


export function graphql(source: string) {
  return (documents as any)[source] ?? {};
}

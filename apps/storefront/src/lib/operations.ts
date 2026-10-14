// Single import point for the storefront's GraphQL operation. Types and the document are generated from
// src/graphql/search.graphql + packages/contracts/schema.graphql by `pnpm --filter @souq/contracts codegen`
// (client preset, documentMode "string", enums as string unions) into src/gql/. Components import from here so
// a codegen config change touches one file. operations.test.ts checks the document matches the .graphql file.
export {
  type Degradation,
  type FiltersInput,
  type Lang,
  type ParserSource,
  type RankSource,
  type Script,
  StorefrontSearchDocument,
  type StorefrontSearchQuery,
  type StorefrontSearchQueryVariables,
  TypedDocumentString,
} from "@/gql/graphql";

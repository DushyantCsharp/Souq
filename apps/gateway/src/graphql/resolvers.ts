// GraphQL is a projection of the same pipeline as REST (executeSearch); nothing here re-ranks or
// re-filters items (CLAUDE.md rule 2).
import { GraphQLError } from "graphql";

import { loadBudgets } from "../config/budgets.js";
import { loadEnv } from "../config/env.js";
import { problemType, toProblemError } from "../errors/problem.js";
import { executeSearch, type SearchDeps } from "../search/execute.js";
import { type ParamNames, validateSearchInput } from "../search/input.js";
import type { Filters, PublicSearchResponse, SearchInput as ValidSearchInput } from "../search/types.js";
import type {
  Degradation,
  FilterField,
  FilterSource,
  Filters as GqlFilters,
  FiltersInput,
  InterpretedFilter,
  ParserSource,
  ProductSearchConnection,
  RankSource,
  Resolvers,
  Script,
} from "../generated/graphql.js";
import type { GraphQLContext } from "./context.js";

const SNAKE_TO_CAMEL: Record<string, string> = { price_min: "priceMin", price_max: "priceMax" };

const GRAPHQL_PARAM_NAMES: ParamNames = {
  location: "body",
  q: "input.q",
  lang: "input.lang",
  limit: "first",
  pageToken: "after",
  filter: (field) => `input.filters.${SNAKE_TO_CAMEL[field] ?? field}`,
};

const RANK_SOURCES: Record<string, RankSource> = { rerank: "RERANK", fused: "FUSED", lexical_tail: "LEXICAL_TAIL" };
const SCRIPTS: Record<string, Script> = { ar: "AR", en: "EN", mixed: "MIXED", arabizi: "ARABIZI" };
const PARSERS: Record<string, ParserSource> = { rule: "RULE", llm: "LLM", cache: "CACHE" };
const DEGRADATIONS: Record<string, Degradation> = {
  rerank: "RERANK",
  llm_parse: "LLM_PARSE",
  knn: "KNN",
  bm25: "BM25",
  semantic_cache: "SEMANTIC_CACHE",
};

const ERROR_CODES: Record<string, string> = {
  "validation-error": "BAD_USER_INPUT",
  "invalid-page-token": "INVALID_PAGE_TOKEN",
  "upstream-timeout": "UPSTREAM_TIMEOUT",
  "search-unavailable": "SEARCH_UNAVAILABLE",
  "bad-gateway": "SEARCH_UNAVAILABLE",
};

let defaultSecret: string | undefined;

function depsFromContext(ctx: GraphQLContext): SearchDeps {
  defaultSecret ??= loadEnv().pageTokenSecret;
  return {
    searchClient: ctx.searchClient,
    pageTokenSecret: ctx.pageTokenSecret ?? defaultSecret,
    budgets: ctx.budgets ?? loadBudgets(),
    cache: ctx.cache ?? null,
    ...(ctx.log ? { log: ctx.log } : {}),
    ...(ctx.annotate ? { annotate: ctx.annotate } : {}),
  };
}

function inputFilters(filters: FiltersInput | null | undefined): Record<string, unknown> | undefined {
  if (!filters) return undefined;
  return {
    price_min: filters.priceMin,
    price_max: filters.priceMax,
    currency: filters.currency,
    brands: filters.brands,
    colours: filters.colours,
    sizes: filters.sizes,
    category: filters.category,
  };
}

function toGqlFilters(filters: Filters): GqlFilters {
  return {
    priceMin: filters.price_min ?? null,
    priceMax: filters.price_max ?? null,
    currency: filters.currency ?? null,
    brands: filters.brands ?? [],
    colours: filters.colours ?? [],
    sizes: filters.sizes ?? [],
    category: filters.category ?? null,
  };
}

function mapEnum<T extends string>(table: Record<string, T>, value: string, what: string): T {
  const mapped = table[value];
  if (mapped === undefined) {
    throw new GraphQLError(`search service returned unknown ${what} "${value}"`, {
      extensions: { code: "SEARCH_UNAVAILABLE", type: problemType("bad-gateway") },
    });
  }
  return mapped;
}

const FIELD_ENUM: Record<keyof Filters, FilterField> = {
  price_min: "PRICE_MIN",
  price_max: "PRICE_MAX",
  currency: "CURRENCY",
  brands: "BRAND",
  colours: "COLOUR",
  sizes: "SIZE",
  category: "CATEGORY",
};

/** Every applied value, tagged EXPLICIT when it came from the request and with the parser otherwise. */
export function filterSources(applied: Filters, explicit: Filters, parser: ParserSource): InterpretedFilter[] {
  const out: InterpretedFilter[] = [];
  for (const field of Object.keys(FIELD_ENUM) as (keyof Filters)[]) {
    const value = applied[field];
    if (value === undefined || value === null) continue;
    const given = explicit[field];
    const values = Array.isArray(value) ? value : [value];
    for (const v of values) {
      const isExplicit = Array.isArray(given) ? given.includes(v as string) : given !== undefined && given === v;
      const source: FilterSource = isExplicit ? "EXPLICIT" : parser;
      out.push({ field: FIELD_ENUM[field], value: String(v), source });
    }
  }
  return out;
}

export function toConnection(response: PublicSearchResponse, input: ValidSearchInput): ProductSearchConnection {
  const parser = mapEnum(PARSERS, response.parsed_query.parser, "parser");
  const cursor = response.next_page_token;
  return {
    edges: response.items.map((item) => ({
      cursor,
      node: {
        sku: item.sku,
        title: item.title,
        description: item.description ?? null,
        brand: item.brand,
        price: { amount: item.price.amount, currency: item.price.currency },
        categoryPath: item.category_path,
        colour: item.colour ?? null,
        size: item.size ?? null,
        score: item.score,
        rankSource: mapEnum(RANK_SOURCES, item.rank_source, "rank_source"),
      },
    })),
    pageInfo: { hasNextPage: cursor !== null, hasPreviousPage: false, startCursor: null, endCursor: cursor },
    facets: response.facets.map((f) => ({ name: f.name, buckets: f.buckets.map((b) => ({ ...b })) })),
    appliedFilters: toGqlFilters(response.applied_filters),
    interpretation: {
      original: response.parsed_query.original,
      normalised: response.parsed_query.normalised,
      rewritten: response.parsed_query.rewritten ?? null,
      detectedLang: response.parsed_query.detected_lang,
      script: mapEnum(SCRIPTS, response.parsed_query.script, "script"),
      parser,
      filters: toGqlFilters(response.parsed_query.filters),
      filterSources: filterSources(response.applied_filters, input.filters, parser),
      unparsedTokens: response.parsed_query.unparsed_tokens,
    },
    // Unknown degradations stay visible over REST; the GraphQL enum only carries the known ones.
    degraded: response.degraded.flatMap((d) => DEGRADATIONS[d] ?? []),
    totalEstimate: response.total_estimate,
  };
}

/** Same problem taxonomy as REST, expressed as GraphQL error extensions. */
export function toGraphQLError(err: unknown): GraphQLError {
  if (err instanceof GraphQLError) return err;
  const problem = toProblemError(err);
  if (problem.statusCode >= 500 && ERROR_CODES[problem.slug] === undefined) {
    // Unexpected failure: let Yoga mask it as "Unexpected error." (and log it).
    throw err;
  }
  return new GraphQLError(problem.detail ?? problem.message, {
    extensions: {
      code: ERROR_CODES[problem.slug] ?? "BAD_USER_INPUT",
      type: problemType(problem.slug),
      status: problem.statusCode,
      ...(problem.errors ? { errors: problem.errors } : {}),
    },
    // No `originalError`: Yoga masks GraphQLErrors that wrap a non-GraphQL error, and these are expected.
  });
}

export const resolvers: Resolvers<GraphQLContext> = {
  Query: {
    async search(_parent, args, ctx) {
      const validation = validateSearchInput(
        {
          q: args.input.q,
          lang: args.input.lang ?? undefined,
          limit: args.first ?? undefined,
          pageToken: args.after ?? undefined,
          filters: inputFilters(args.input.filters),
        },
        GRAPHQL_PARAM_NAMES,
      );
      if (!validation.ok) {
        throw new GraphQLError("Invalid search arguments", {
          extensions: { code: "BAD_USER_INPUT", type: problemType("validation-error"), errors: validation.errors },
        });
      }
      try {
        const { response } = await executeSearch(depsFromContext(ctx), validation.input, {
          ...(ctx.requestId ? { requestId: ctx.requestId } : {}),
          ...(ctx.signal ? { signal: ctx.signal } : {}),
        });
        return toConnection(response, validation.input);
      } catch (err) {
        throw toGraphQLError(err);
      }
    },
  },
};

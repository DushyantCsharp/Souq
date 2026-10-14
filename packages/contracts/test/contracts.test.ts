// Cross-contract consistency: the gateway maps public <-> internal <-> GraphQL by name, so these
// invariants are what keep that mapping a pass-through.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, it } from "node:test";

import { buildSchema, type GraphQLEnumType, type GraphQLInputObjectType, isEnumType } from "graphql";
import { parse } from "yaml";

import { CONTRACTS_DIR, internalSpec } from "../scripts/codegen.ts";

type Schema = { properties?: Record<string, unknown>; required?: string[]; enum?: string[] };
type Spec = { components: { schemas: Record<string, Schema> } };

const publicSpec = parse(readFileSync(join(CONTRACTS_DIR, "openapi.yaml"), "utf8")) as Spec;
const searchSpec = JSON.parse(readFileSync(internalSpec("search-internal"), "utf8")) as Spec;
const feedsSpec = JSON.parse(readFileSync(internalSpec("feeds-internal"), "utf8")) as Spec;
const graphql = buildSchema(readFileSync(join(CONTRACTS_DIR, "schema.graphql"), "utf8"));

const props = (s: Schema | undefined): string[] => Object.keys(s?.properties ?? {}).sort();
const enumValues = (name: string): string[] =>
  (graphql.getType(name) as GraphQLEnumType).getValues().map((v) => v.name).sort();
const enumOf = (schema: Schema, field: string): string[] =>
  [...(((schema.properties?.[field] as Schema | undefined)?.enum ?? []) as string[])].sort();

describe("public vs internal search contract", () => {
  it("Filters have identical field names (gateway forwards them unchanged)", () => {
    assert.deepEqual(props(publicSpec.components.schemas.Filters), props(searchSpec.components.schemas.Filters));
  });

  it("SearchResponse exposes everything the brief promises", () => {
    const required = publicSpec.components.schemas.SearchResponse?.required ?? [];
    for (const field of ["items", "facets", "applied_filters", "parsed_query", "degraded", "next_page_token"]) {
      assert.ok(required.includes(field), `SearchResponse.${field} must be required`);
    }
  });

  it("items carry the internal item fields", () => {
    assert.deepEqual(props(publicSpec.components.schemas.SearchItem), props(searchSpec.components.schemas.SearchItem));
  });
});

describe("GraphQL vs REST", () => {
  it("enums mirror the REST enums (upper-cased)", () => {
    const item = publicSpec.components.schemas.SearchItem as Schema;
    const parsed = publicSpec.components.schemas.ParsedQuery as Schema;
    assert.deepEqual(enumValues("RankSource"), enumOf(item, "rank_source").map((v) => v.toUpperCase()));
    assert.deepEqual(enumValues("Script"), enumOf(parsed, "script").map((v) => v.toUpperCase()));
    assert.deepEqual(enumValues("ParserSource"), enumOf(parsed, "parser").map((v) => v.toUpperCase()));
  });

  it("Degradation covers every degraded value services/search can emit", () => {
    const degraded = (searchSpec.components.schemas.SearchResponse?.properties?.degraded ?? {}) as {
      items?: Schema;
    };
    const internal = degraded.items?.enum ?? ["rerank", "llm_parse", "knn", "bm25", "semantic_cache"];
    assert.deepEqual(enumValues("Degradation"), [...internal].map((v) => v.toUpperCase()).sort());
  });

  it("FiltersInput matches REST Filters field-for-field (camelCase)", () => {
    const input = graphql.getType("FiltersInput") as GraphQLInputObjectType;
    const camel = props(publicSpec.components.schemas.Filters).map((f) =>
      f.replace(/_([a-z])/g, (_m, c: string) => c.toUpperCase()),
    );
    assert.deepEqual(Object.keys(input.getFields()).sort(), camel.sort());
  });

  it("every enum in the schema is non-empty", () => {
    for (const type of Object.values(graphql.getTypeMap())) {
      if (isEnumType(type) && !type.name.startsWith("__")) assert.ok(type.getValues().length > 0, type.name);
    }
  });
});

describe("feeds contract", () => {
  it("status and row-error shapes match services/feeds field-for-field", () => {
    const pub = publicSpec.components.schemas;
    const internal = feedsSpec.components.schemas;
    assert.deepEqual(props(pub.FeedStatus), props(internal.FeedStatus));
    assert.deepEqual(props(pub.FeedRowError), props(internal.FeedRowError));
    assert.deepEqual(props(pub.FeedAccepted), props(internal.FeedAccepted));
  });

  it("reject codes are exactly the CLAUDE.md list", () => {
    assert.deepEqual([...(publicSpec.components.schemas.FeedRowErrorCode?.enum ?? [])].sort(), [
      "duplicate_sku_in_feed",
      "invalid_category",
      "invalid_currency",
      "invalid_json",
      "invalid_price",
      "malformed_row",
      "missing_field",
      "too_long",
    ]);
  });
});

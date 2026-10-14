/** @jest-environment node */
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "@jest/globals";
import { documentText } from "./gateway";
import { StorefrontSearchDocument } from "./operations";

// GraphQL source → comparable token stream (comments, commas and whitespace are insignificant in GraphQL).
function tokens(source: string): string {
  return source
    .replace(/#[^\n]*/g, " ")
    .replace(/,/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

describe("StorefrontSearchDocument", () => {
  it("matches src/graphql/search.graphql", () => {
    const file = readFileSync(path.join(process.cwd(), "src/graphql/search.graphql"), "utf8");
    expect(tokens(documentText(StorefrontSearchDocument))).toBe(tokens(file));
  });
});

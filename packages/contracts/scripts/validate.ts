// Validates the contracts themselves (run as this package's build and lint):
//   - openapi.yaml against Redocly's "recommended" ruleset (OpenAPI 3.1 structure + hygiene)
//   - internal service specs (real export if present, else provisional) against "minimal"
//   - schema.graphql builds into a valid schema and every storefront document validates against it
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";

import { createConfig, formatProblems, getTotals, lint } from "@redocly/openapi-core";
import { buildSchema, parse, validate, validateSchema } from "graphql";

import { CONTRACTS_DIR, internalSpec, REPO_ROOT } from "./codegen.ts";

let failed = false;

type Rules = Record<string, "error" | "warn" | "off">;

// Public edge: every 4xx is problem+json (RFC 9457). The localhost server entry is intentional (dev docs).
const PUBLIC_RULES: Rules = { "operation-4xx-problem-details-rfc7807": "error", "no-server-example.com": "off" };
// FastAPI exports: no servers/summaries/security by design; we only care that the document is valid.
const INTERNAL_RULES: Rules = { "no-empty-servers": "off", "operation-summary": "off", "security-defined": "off" };

async function lintOpenApi(file: string, ruleset: "recommended" | "minimal", rules: Rules): Promise<void> {
  const config = await createConfig({ extends: [ruleset], rules });
  const problems = await lint({ ref: file, config });
  const totals = getTotals(problems);
  if (problems.length > 0) {
    formatProblems(problems, { format: "stylish", totals, version: "souq" });
  }
  if (totals.errors > 0) failed = true;
  console.warn(`${relative(REPO_ROOT, file)}: ${totals.errors} error(s), ${totals.warnings} warning(s)`);
}

function graphqlDocuments(dir: string): string[] {
  if (!existsSync(dir)) return [];
  return readdirSync(dir, { recursive: true, encoding: "utf8" })
    .filter((f) => f.endsWith(".graphql"))
    .map((f) => join(dir, f));
}

function checkGraphql(): void {
  const schema = buildSchema(readFileSync(join(CONTRACTS_DIR, "schema.graphql"), "utf8"));
  const schemaErrors = validateSchema(schema);
  for (const err of schemaErrors) console.error(`schema.graphql: ${err.message}`);
  if (schemaErrors.length > 0) failed = true;

  for (const file of graphqlDocuments(join(REPO_ROOT, "apps/storefront/src/graphql"))) {
    const errors = validate(schema, parse(readFileSync(file, "utf8")));
    for (const err of errors) console.error(`${relative(REPO_ROOT, file)}: ${err.message}`);
    if (errors.length > 0) failed = true;
  }
  console.warn(`schema.graphql: ${schemaErrors.length} error(s)`);
}

await lintOpenApi(join(CONTRACTS_DIR, "openapi.yaml"), "recommended", PUBLIC_RULES);
await lintOpenApi(internalSpec("search-internal"), "minimal", INTERNAL_RULES);
await lintOpenApi(internalSpec("feeds-internal"), "minimal", INTERNAL_RULES);
checkGraphql();

if (failed) {
  console.error("contracts: validation failed");
  process.exit(1);
}

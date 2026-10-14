// Latency budgets come from config/budgets.yaml (CLAUDE.md rule 3), never from literals here.
import { readFileSync } from "node:fs";
import path from "node:path";
import { parse } from "yaml";
import { z } from "zod";

const budgetsSchema = z.object({
  gateway: z.looseObject({ search_timeout_ms: z.number().positive() }),
  storefront: z.looseObject({ gateway_timeout_ms: z.number().positive() }).optional(),
});

export function configDir(): string {
  // Dev/test run from apps/storefront; containers set SOUQ_CONFIG_DIR to the mounted config.
  return process.env.SOUQ_CONFIG_DIR ?? path.resolve(process.cwd(), "../../config");
}

let cached: number | undefined;

/**
 * How long the storefront waits for the gateway's GraphQL response. Uses `storefront.gateway_timeout_ms`
 * when present; otherwise the gateway's own upstream timeout, which is the longest a healthy gateway takes
 * before answering (with a degraded or error response) anyway.
 */
export function gatewayTimeoutMs(): number {
  if (cached !== undefined) return cached;
  const file = path.join(configDir(), "budgets.yaml");
  const budgets = budgetsSchema.parse(parse(readFileSync(file, "utf8")));
  cached = budgets.storefront?.gateway_timeout_ms ?? budgets.gateway.search_timeout_ms;
  return cached;
}

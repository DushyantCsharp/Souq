// Latency budgets come only from config/budgets.yaml (CLAUDE.md rule 3); nothing here hard-codes a budget.
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { parse } from "yaml";
import { z } from "zod";

const GatewayBudgets = z.looseObject({
  search_timeout_ms: z.number().positive(),
  feeds_timeout_ms: z.number().positive(),
  response_cache_ttl_s: z.number().nonnegative(),
  // Optional: absent means "no extra command timeout" / "do not cache degraded responses".
  cache_timeout_ms: z.number().positive().optional(),
  degraded_response_cache_ttl_s: z.number().nonnegative().optional(),
});

const SearchBudgets = z.looseObject({
  // OpenSearch time unit string ("5m"); page tokens expire with the PIT they point into.
  pit_keep_alive: z.string().min(1),
});

const BudgetsFile = z.looseObject({ gateway: GatewayBudgets, search: SearchBudgets });

export interface Budgets {
  gateway: z.infer<typeof GatewayBudgets>;
  search: z.infer<typeof SearchBudgets>;
  /** `search.pit_keep_alive` in seconds. */
  pageTokenTtlSeconds: number;
}

const UNIT_SECONDS: Record<string, number> = { ms: 0.001, s: 1, m: 60, h: 3600, d: 86400 };

/** Parses an OpenSearch time value such as `5m`, `30s`, `1h`, `500ms` into seconds. */
export function parseTimeValueSeconds(value: string): number {
  const match = /^(\d+(?:\.\d+)?)(ms|s|m|h|d)$/.exec(value.trim());
  const unit = match?.[2];
  if (!match || unit === undefined) throw new Error(`unsupported time value ${JSON.stringify(value)}`);
  return Number(match[1]) * (UNIT_SECONDS[unit] ?? Number.NaN);
}

/** SOUQ_CONFIG_DIR, else the nearest `config/budgets.yaml` above this file (works from src/ and dist/). */
export function configDir(): string {
  const fromEnv = process.env.SOUQ_CONFIG_DIR;
  if (fromEnv) return resolve(fromEnv);
  let dir = dirname(fileURLToPath(import.meta.url));
  for (;;) {
    if (existsSync(join(dir, "config", "budgets.yaml"))) return join(dir, "config");
    const parent = dirname(dir);
    if (parent === dir) throw new Error("config/budgets.yaml not found; set SOUQ_CONFIG_DIR");
    dir = parent;
  }
}

export function parseBudgets(yamlText: string): Budgets {
  const raw = BudgetsFile.parse(parse(yamlText));
  return {
    gateway: raw.gateway,
    search: raw.search,
    pageTokenTtlSeconds: parseTimeValueSeconds(raw.search.pit_keep_alive),
  };
}

export function loadBudgetsFrom(dir: string): Budgets {
  return parseBudgets(readFileSync(join(dir, "budgets.yaml"), "utf8"));
}

let cached: Budgets | undefined;

/** Process-wide budgets (read once). */
export function loadBudgets(): Budgets {
  cached ??= loadBudgetsFrom(configDir());
  return cached;
}

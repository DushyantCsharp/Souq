// One canonical form for (q, lang, filters) shared by cache keys and page-token query binding, so that
// cosmetic differences (key order, array order, whitespace) neither split the cache nor break paging.
import { createHash } from "node:crypto";

/** Trim and collapse internal whitespace. Unicode/Arabic normalisation belongs to services/search. */
export function canonicalQuery(q: string): string {
  return q.trim().replace(/\s+/gu, " ");
}

export function canonicalLang(lang: string | null | undefined): string {
  return (lang ?? "auto").toLowerCase();
}

type FilterValue = string | number | boolean | readonly (string | number)[] | null | undefined;

/**
 * Drops empty values, de-duplicates and sorts multi-valued fields (they are OR-ed, so order is
 * meaningless) and sorts keys. Works on any filter-like object so unknown future fields still count.
 */
export function canonicalFilters(filters: Readonly<Record<string, unknown>> | null | undefined): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  if (!filters) return out;
  for (const key of Object.keys(filters).sort()) {
    const value = filters[key] as FilterValue;
    if (value === null || value === undefined || value === "") continue;
    if (Array.isArray(value)) {
      const items = [...new Set(value.map(String))].sort();
      if (items.length > 0) out[key] = items;
      continue;
    }
    out[key] = value;
  }
  return out;
}

/** JSON with recursively sorted object keys. */
export function canonicalJson(value: unknown): string {
  return JSON.stringify(sortKeys(value));
}

function sortKeys(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortKeys);
  if (value && typeof value === "object") {
    const obj = value as Record<string, unknown>;
    return Object.fromEntries(
      Object.keys(obj)
        .sort()
        .filter((k) => obj[k] !== undefined)
        .map((k) => [k, sortKeys(obj[k])]),
    );
  }
  return value;
}

export function sha256(text: string): string {
  return createHash("sha256").update(text, "utf8").digest("hex");
}

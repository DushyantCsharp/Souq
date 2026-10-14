// Validation shared by REST (`filters[brands]=nike`) and GraphQL (`input.filters.brands`): both produce
// the same SearchInput, so caching, page-token binding and upstream requests cannot diverge.
import type { InvalidParam } from "../errors/problem.js";
import { canonicalQuery } from "./canonical.js";
import {
  DEFAULT_LIMIT,
  type Filters,
  LANGS,
  type Lang,
  MAX_LIMIT,
  MAX_PAGE_TOKEN_LENGTH,
  MAX_QUERY_LENGTH,
  type SearchInput,
} from "./types.js";

export const FILTER_FIELDS = ["price_min", "price_max", "currency", "brands", "colours", "sizes", "category"] as const;
export type FilterField = (typeof FILTER_FIELDS)[number];

const MULTI_FIELDS = { brands: 128, colours: 64, sizes: 32 } as const;
const MAX_VALUES_PER_FILTER = 20;
const MAX_CATEGORY_LENGTH = 512;

export interface RawSearchInput {
  q?: unknown;
  lang?: unknown;
  limit?: unknown;
  pageToken?: unknown;
  filters?: Readonly<Record<string, unknown>>;
}

export interface ParamNames {
  location: InvalidParam["location"];
  q: string;
  lang: string;
  limit: string;
  pageToken: string;
  filter: (field: string) => string;
}

export const REST_PARAM_NAMES: ParamNames = {
  location: "query",
  q: "q",
  lang: "lang",
  limit: "limit",
  pageToken: "page_token",
  filter: (field) => `filters[${field}]`,
};

function single(value: unknown): unknown {
  // Repeated scalar query params arrive as arrays; the last one wins, as in most HTTP frameworks.
  return Array.isArray(value) ? value[value.length - 1] : value;
}

function toNumber(value: unknown): number | undefined {
  const v = single(value);
  if (typeof v === "number") return Number.isFinite(v) ? v : undefined;
  if (typeof v === "string" && v.trim() !== "") {
    const n = Number(v);
    return Number.isFinite(n) ? n : undefined;
  }
  return undefined;
}

function toStringList(value: unknown): string[] | undefined {
  const list = Array.isArray(value) ? value : [value];
  if (!list.every((v) => typeof v === "string")) return undefined;
  return [...new Set((list as string[]).map((v) => v.trim()).filter((v) => v.length > 0))];
}

export function validateFilters(
  raw: Readonly<Record<string, unknown>> | undefined,
  names: ParamNames,
  errors: InvalidParam[],
): Filters {
  const filters: Filters = {};
  if (!raw) return filters;
  const fail = (field: string, detail: string): void => {
    errors.push({ location: names.location, name: names.filter(field), detail });
  };

  for (const [field, value] of Object.entries(raw)) {
    if (value === undefined || value === null) continue;
    switch (field) {
      case "price_min":
      case "price_max": {
        const n = toNumber(value);
        if (n === undefined || n < 0) fail(field, "must be a non-negative number");
        else filters[field] = n;
        break;
      }
      case "currency": {
        const v = single(value);
        const code = typeof v === "string" ? v.trim().toUpperCase() : "";
        if (!/^[A-Z]{3}$/.test(code)) fail(field, "must be a 3-letter ISO 4217 code");
        else filters.currency = code;
        break;
      }
      case "category": {
        const v = single(value);
        const category = typeof v === "string" ? v.trim() : "";
        if (category.length === 0 || category.length > MAX_CATEGORY_LENGTH) {
          fail(field, `must be 1..${MAX_CATEGORY_LENGTH} characters`);
        } else filters.category = category;
        break;
      }
      case "brands":
      case "colours":
      case "sizes": {
        const values = toStringList(value);
        const maxLength = MULTI_FIELDS[field];
        if (values === undefined) fail(field, "must be a list of strings");
        else if (values.length > MAX_VALUES_PER_FILTER) fail(field, `at most ${MAX_VALUES_PER_FILTER} values`);
        else if (values.some((v) => v.length > maxLength)) fail(field, `values must be at most ${maxLength} characters`);
        else if (values.length > 0) filters[field] = field === "colours" ? values.map((v) => v.toLowerCase()) : values;
        break;
      }
      default:
        fail(field, `unknown filter; expected one of ${FILTER_FIELDS.join(", ")}`);
    }
  }
  if (filters.price_min !== undefined && filters.price_max !== undefined && filters.price_min > filters.price_max) {
    fail("price_min", "must not exceed price_max");
  }
  return filters;
}

export type ValidationResult = { ok: true; input: SearchInput } | { ok: false; errors: InvalidParam[] };

export function validateSearchInput(raw: RawSearchInput, names: ParamNames = REST_PARAM_NAMES): ValidationResult {
  const errors: InvalidParam[] = [];
  const fail = (name: string, detail: string): void => {
    errors.push({ location: names.location, name, detail });
  };

  const rawQ = single(raw.q);
  const q = typeof rawQ === "string" ? canonicalQuery(rawQ) : "";
  if (q.length === 0 || q.length > MAX_QUERY_LENGTH) fail(names.q, `is required, 1..${MAX_QUERY_LENGTH} characters`);

  let lang: Lang = "auto";
  const rawLang = single(raw.lang);
  if (rawLang !== undefined && rawLang !== null && rawLang !== "") {
    const candidate = typeof rawLang === "string" ? rawLang.toLowerCase() : "";
    if ((LANGS as readonly string[]).includes(candidate)) lang = candidate as Lang;
    else fail(names.lang, `must be one of ${LANGS.join(", ")}`);
  }

  let limit = DEFAULT_LIMIT;
  if (raw.limit !== undefined && raw.limit !== null && raw.limit !== "") {
    const n = toNumber(raw.limit);
    if (n === undefined || !Number.isInteger(n) || n < 1 || n > MAX_LIMIT) fail(names.limit, `must be an integer 1..${MAX_LIMIT}`);
    else limit = n;
  }

  let pageToken: string | null = null;
  const rawToken = single(raw.pageToken);
  if (rawToken !== undefined && rawToken !== null && rawToken !== "") {
    if (typeof rawToken !== "string" || rawToken.length > MAX_PAGE_TOKEN_LENGTH) {
      fail(names.pageToken, `must be a string of at most ${MAX_PAGE_TOKEN_LENGTH} characters`);
    } else pageToken = rawToken;
  }

  const filters = validateFilters(raw.filters, names, errors);
  return errors.length > 0 ? { ok: false, errors } : { ok: true, input: { q, lang, filters, limit, pageToken } };
}

/**
 * Collects deepObject `filters[field]=value` keys from a flat query map (Fastify does not expand
 * brackets). A bare `filters=...` or nested brackets are reported rather than silently ignored.
 */
export function filtersFromQuery(
  query: Readonly<Record<string, unknown>>,
  errors: InvalidParam[],
): Record<string, unknown> {
  const filters: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(query)) {
    if (key === "filters") {
      errors.push({ location: "query", name: key, detail: "use deepObject form, e.g. filters[brands]=nike" });
      continue;
    }
    if (!key.startsWith("filters[")) continue;
    const match = /^filters\[([a-z_]+)\]$/.exec(key);
    if (!match?.[1]) {
      errors.push({ location: "query", name: key, detail: "malformed filter parameter" });
      continue;
    }
    const field = match[1];
    const prior = filters[field];
    // `filters[brands]=a&filters[brands]=b` may arrive as one array or (after merging) several values.
    filters[field] = prior === undefined ? value : [prior, value].flat();
  }
  return filters;
}

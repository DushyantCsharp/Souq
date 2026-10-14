import type { Budgets } from "../config/budgets.js";
import type { ResponseCache } from "../cache/response-cache.js";
import type { SearchClient } from "../search/client.js";

/**
 * Per-request GraphQL context. Only `searchClient` is required so tests can inject a fake; the rest
 * defaults from the environment (PAGE_TOKEN_SECRET, config/budgets.yaml) and no cache.
 */
export interface GraphQLContext {
  searchClient: SearchClient;
  pageTokenSecret?: string;
  budgets?: Budgets;
  cache?: ResponseCache | null;
  requestId?: string;
  signal?: AbortSignal;
  log?: { warn(obj: object, msg: string): void };
  annotate?: (q: string) => void;
}

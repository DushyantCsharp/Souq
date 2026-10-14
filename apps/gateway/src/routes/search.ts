import type { FastifyInstance } from "fastify";

import { type InvalidParam, validationProblem } from "../errors/problem.js";
import { executeSearch, type SearchDeps } from "../search/execute.js";
import { filtersFromQuery, validateSearchInput } from "../search/input.js";

export function registerSearchRoutes(app: FastifyInstance, deps: SearchDeps): void {
  app.get("/v1/search", async (request, reply) => {
    const query = (request.query ?? {}) as Record<string, unknown>;
    const filterErrors: InvalidParam[] = [];
    const filters = filtersFromQuery(query, filterErrors);
    const result = validateSearchInput({
      q: query.q,
      lang: query.lang,
      limit: query.limit,
      pageToken: query.page_token,
      filters,
    });
    const errors = [...filterErrors, ...(result.ok ? [] : result.errors)];
    if (!result.ok || errors.length > 0) throw validationProblem(errors);

    const outcome = await executeSearch(deps, result.input, { requestId: request.id });
    return reply.header("x-souq-cache", outcome.cache).send(outcome.response);
  });
}

import { randomUUID } from "node:crypto";

import Fastify, { type FastifyInstance, type FastifyServerOptions } from "fastify";
import type { Redis } from "ioredis";

import { createApiKeyVerifier } from "./auth/api-key.js";
import type { ResponseCache } from "./cache/response-cache.js";
import type { Budgets } from "./config/budgets.js";
import type { GatewayEnv } from "./config/env.js";
import { registerProblemHandlers, requestPath } from "./errors/problem.js";
import type { FeedsClient } from "./feeds/client.js";
import { registerGraphQL } from "./graphql/yoga.js";
import { registerRateLimit } from "./rate-limit.js";
import { registerFeedRoutes } from "./routes/feeds.js";
import { type ReadinessCheck, registerHealthRoutes } from "./routes/health.js";
import { registerSearchRoutes } from "./routes/search.js";
import type { SearchClient } from "./search/client.js";

export interface AppDeps {
  env: GatewayEnv;
  budgets: Budgets;
  searchClient: SearchClient;
  feedsClient: FeedsClient;
  /** Shared by the response cache and the rate limiter; null runs without Redis. */
  redis?: Redis | null;
  cache?: ResponseCache | null;
  readiness?: { required?: Record<string, ReadinessCheck>; informational?: Record<string, ReadinessCheck> };
  logger?: FastifyServerOptions["logger"];
  /** See SearchDeps.annotate. */
  annotate?: (q: string) => void;
}

const REQUEST_ID = /^[A-Za-z0-9._:-]{1,128}$/;

export async function buildApp(deps: AppDeps): Promise<FastifyInstance> {
  const app = Fastify({
    logger: deps.logger ?? {
      level: deps.env.logLevel,
      // Query strings carry search text (PII, CLAUDE.md rule 6): log paths only.
      serializers: {
        req: (req: { method: string; url: string; id: string }) => ({
          method: req.method,
          path: requestPath(req.url),
          reqId: req.id,
        }),
      },
    },
    // Honour an upstream request id (storefront, load balancer) when it is well-formed.
    genReqId: (req) => {
      const incoming = req.headers["x-request-id"];
      return typeof incoming === "string" && REQUEST_ID.test(incoming) ? incoming : randomUUID();
    },
    trustProxy: deps.env.trustProxy,
  });

  app.addHook("onSend", async (request, reply) => {
    reply.header("x-request-id", request.id);
  });
  registerProblemHandlers(app);

  const verifier = createApiKeyVerifier(deps.env.apiKeys);
  if (!verifier.configured) app.log.warn("GATEWAY_API_KEYS is empty: feed endpoints will reject every request");
  if (deps.env.pageTokenSecretEphemeral) {
    app.log.warn("PAGE_TOKEN_SECRET unset: using a per-process secret (tokens die with this process)");
  }

  await registerRateLimit(app, {
    max: deps.env.rateLimit.max,
    timeWindow: deps.env.rateLimit.timeWindow,
    verifier,
    redis: deps.redis ?? null,
  });

  const searchDeps = {
    searchClient: deps.searchClient,
    pageTokenSecret: deps.env.pageTokenSecret,
    budgets: deps.budgets,
    cache: deps.cache ?? null,
    log: app.log,
    ...(deps.annotate ? { annotate: deps.annotate } : {}),
  };
  registerSearchRoutes(app, searchDeps);
  registerGraphQL(app, {
    searchClient: deps.searchClient,
    pageTokenSecret: deps.env.pageTokenSecret,
    budgets: deps.budgets,
    cache: deps.cache ?? null,
    ...(deps.annotate ? { annotate: deps.annotate } : {}),
    graphiql: deps.env.nodeEnv !== "production",
  });
  await registerFeedRoutes(app, {
    feedsClient: deps.feedsClient,
    verifier,
    maxUploadBytes: deps.env.feedsMaxUploadBytes,
  });
  registerHealthRoutes(app, {
    required: deps.readiness?.required ?? {},
    informational: deps.readiness?.informational ?? {},
  });

  return app;
}

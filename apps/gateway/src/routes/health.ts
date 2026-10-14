import type { FastifyInstance } from "fastify";

export type ReadinessCheck = () => Promise<boolean>;

export interface HealthDeps {
  /** Checks that gate readiness (search must be reachable to serve anything). */
  required: Record<string, ReadinessCheck>;
  /** Reported but never fail readiness (Redis: cache and rate limits fail open). */
  informational: Record<string, ReadinessCheck>;
}

async function runChecks(checks: Record<string, ReadinessCheck>): Promise<Record<string, boolean>> {
  const entries = await Promise.all(
    Object.entries(checks).map(async ([name, check]) => [name, await check().catch(() => false)] as const),
  );
  return Object.fromEntries(entries);
}

export function registerHealthRoutes(app: FastifyInstance, deps: HealthDeps): void {
  const noRateLimit = { config: { rateLimit: false } };

  app.get("/healthz", noRateLimit, async () => ({ status: "ok" as const }));

  app.get("/readyz", noRateLimit, async (_request, reply) => {
    const [required, informational] = await Promise.all([runChecks(deps.required), runChecks(deps.informational)]);
    const ready = Object.values(required).every(Boolean);
    const checks = Object.fromEntries(
      Object.entries({ ...informational, ...required }).map(([name, ok]) => [name, ok ? "ok" : "failing"]),
    );
    return reply.code(ready ? 200 : 503).send({ status: ready ? "ready" : "not_ready", checks });
  });
}

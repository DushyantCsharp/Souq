import { Redis } from "ioredis";

import type { Budgets } from "../config/budgets.js";
import type { CacheLogger } from "./response-cache.js";

/**
 * Shared by the response cache and the rate limiter. The offline queue is disabled so that while Redis
 * is down commands fail immediately (and both users fail open) instead of piling up behind reconnects.
 */
export function createRedis(url: string, budgets: Budgets, log: CacheLogger): Redis {
  const redis = new Redis(url, {
    lazyConnect: true,
    enableOfflineQueue: false,
    maxRetriesPerRequest: 1,
    ...(budgets.gateway.cache_timeout_ms !== undefined ? { commandTimeout: budgets.gateway.cache_timeout_ms } : {}),
    // Reconnect back-off, not a request budget: caps how often a dead Redis is re-dialled.
    retryStrategy: (attempt) => Math.min(attempt * 200, 5000),
  });
  let lastWarn = 0;
  redis.on("error", (err: Error) => {
    // ioredis emits on every failed reconnect; one line per 30 s is enough to see the outage.
    const now = Date.now();
    if (now - lastWarn > 30_000) {
      lastWarn = now;
      log.warn({ err: { message: err.message } }, "redis unavailable; cache and rate limits fail open");
    }
  });
  return redis;
}

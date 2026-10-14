// Response cache. Fails open: a Redis outage costs latency, never availability.
import type { Redis } from "ioredis";

export interface CacheLogger {
  warn(obj: object, msg: string): void;
}

export interface ResponseCache {
  get(key: string): Promise<string | null>;
  /** ttlSeconds <= 0 means "do not store". */
  set(key: string, value: string, ttlSeconds: number): Promise<void>;
}

export function createRedisResponseCache(redis: Redis, log: CacheLogger): ResponseCache {
  return {
    async get(key) {
      try {
        return await redis.get(key);
      } catch (err) {
        log.warn({ err }, "response cache read failed; serving uncached");
        return null;
      }
    },
    async set(key, value, ttlSeconds) {
      if (ttlSeconds <= 0) return;
      try {
        await redis.set(key, value, "EX", Math.ceil(ttlSeconds));
      } catch (err) {
        log.warn({ err }, "response cache write failed");
      }
    },
  };
}

/** Process-local cache for tests and Redis-less local runs. */
export function createMemoryResponseCache(now: () => number = Date.now): ResponseCache & { size(): number } {
  const entries = new Map<string, { value: string; expiresAt: number }>();
  return {
    get(key) {
      const entry = entries.get(key);
      if (!entry) return Promise.resolve(null);
      if (entry.expiresAt <= now()) {
        entries.delete(key);
        return Promise.resolve(null);
      }
      return Promise.resolve(entry.value);
    },
    set(key, value, ttlSeconds) {
      if (ttlSeconds > 0) entries.set(key, { value, expiresAt: now() + ttlSeconds * 1000 });
      return Promise.resolve();
    },
    size: () => entries.size,
  };
}

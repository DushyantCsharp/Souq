import { describe, expect, it, jest } from "@jest/globals";
import type { Redis } from "ioredis";

import { createMemoryResponseCache, createRedisResponseCache } from "../src/cache/response-cache.js";

describe("response cache", () => {
  it("fails open when Redis errors", async () => {
    const warn = jest.fn();
    const broken = {
      get: () => Promise.reject(new Error("ECONNREFUSED")),
      set: () => Promise.reject(new Error("ECONNREFUSED")),
    } as unknown as Redis;
    const cache = createRedisResponseCache(broken, { warn });
    await expect(cache.get("k")).resolves.toBeNull();
    await expect(cache.set("k", "v", 60)).resolves.toBeUndefined();
    expect(warn).toHaveBeenCalledTimes(2);
  });

  it("stores with EX ttl and skips non-positive ttls", async () => {
    const set = jest.fn((..._args: unknown[]) => Promise.resolve("OK"));
    const cache = createRedisResponseCache({ set } as unknown as Redis, { warn: jest.fn() });
    await cache.set("k", "v", 59.2);
    await cache.set("k", "v", 0);
    expect(set).toHaveBeenCalledTimes(1);
    expect(set).toHaveBeenCalledWith("k", "v", "EX", 60);
  });

  it("memory cache honours expiry", async () => {
    let now = 1_000;
    const cache = createMemoryResponseCache(() => now);
    await cache.set("k", "v", 1);
    await expect(cache.get("k")).resolves.toBe("v");
    now += 1_000;
    await expect(cache.get("k")).resolves.toBeNull();
  });
});

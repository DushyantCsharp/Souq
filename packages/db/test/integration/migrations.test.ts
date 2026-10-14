// Runs every migration up, down and up again against a throwaway database on the compose Postgres
// (DATABASE_URL, default localhost:55432), then checks the behaviour other packages rely on.
import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { after, before, describe, it } from "node:test";
import pg from "pg";
import { resolveDatabaseUrl, runMigrations } from "../../src/index.ts";

const adminUrl = resolveDatabaseUrl();
const dbName = `souq_migtest_${randomBytes(4).toString("hex")}`;
const testUrl = (() => {
  const u = new URL(adminUrl);
  u.pathname = `/${dbName}`;
  return u.toString();
})();
const quiet = () => undefined;

async function withClient<T>(url: string, fn: (c: pg.Client) => Promise<T>): Promise<T> {
  const client = new pg.Client({ connectionString: url });
  await client.connect();
  try {
    return await fn(client);
  } finally {
    await client.end();
  }
}

async function tables(c: pg.Client): Promise<string[]> {
  const r = await c.query<{ tablename: string }>(
    "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'pgmigrations' ORDER BY 1",
  );
  return r.rows.map((row) => row.tablename);
}

describe("migrations against Postgres", () => {
  before(async () => {
    await withClient(adminUrl, (c) => c.query(`CREATE DATABASE ${dbName}`));
  });
  after(async () => {
    await withClient(adminUrl, (c) => c.query(`DROP DATABASE IF EXISTS ${dbName} WITH (FORCE)`));
  });

  it("run up (incl. next), down to empty, and up again", async () => {
    await runMigrations({ direction: "up", includeNext: true, databaseUrl: testUrl, log: quiet });
    await runMigrations({
      direction: "down",
      count: Infinity,
      includeNext: true,
      databaseUrl: testUrl,
      log: quiet,
    });
    await withClient(testUrl, async (c) => assert.deepEqual(await tables(c), []));
    await runMigrations({ direction: "up", databaseUrl: testUrl, log: quiet });
    await withClient(testUrl, async (c) =>
      assert.deepEqual(await tables(c), [
        "feed_imports",
        "feed_row_errors",
        "outbox",
        "product_texts",
        "products",
        "query_log",
      ]),
    );
  });

  it("category_ltree slugifies display paths and rejects empty labels", async () => {
    await withClient(testUrl, async (c) => {
      const r = await c.query<{ a: string; b: string | null; c: string | null }>(
        `SELECT category_ltree('Clothing, Shoes & Jewelry > Men > Shoes')::text AS a,
                category_ltree('Audio >  > Headphones')::text AS b,
                category_ltree('!!! > ???')::text AS c`,
      );
      assert.deepEqual(r.rows[0], { a: "clothing_shoes_and_jewelry.men.shoes", b: null, c: null });
    });
  });

  it("versions only move on effective changes and product_document reflects them", async () => {
    await withClient(testUrl, async (c) => {
      await c.query("BEGIN");
      try {
        await c.query(`INSERT INTO products (sku, brand, category_path, price, currency, colour, attributes)
                       VALUES ('T1', 'Sony', category_ltree('Electronics > Audio > Headphones'), 199.5, 'AED',
                               'black', '{"size": "M"}')`);
        await c.query(`INSERT INTO product_texts (sku, lang, title) VALUES ('T1', 'en', 'Sony WH-1000XM5'),
                                                                       ('T1', 'ar', 'سماعات سوني')`);
        const version = async () =>
          Number((await c.query<{ version: string }>("SELECT version FROM products")).rows[0]?.version);
        const v0 = await version();
        await c.query("UPDATE products SET price = 199.50 WHERE sku = 'T1'");
        await c.query("UPDATE product_texts SET title = 'Sony WH-1000XM5' WHERE sku = 'T1' AND lang = 'en'");
        assert.equal(await version(), v0, "no-op writes must not bump the version");
        await c.query("UPDATE products SET price = 189 WHERE sku = 'T1'");
        assert.equal(await version(), v0 + 1);
        await c.query("UPDATE product_texts SET description = 'ANC' WHERE sku = 'T1' AND lang = 'en'");
        assert.equal(await version(), v0 + 2, "a text change is a product change");

        // Expand phase: old writers (attributes.size) and new writers (size column) stay in sync.
        await c.query(`UPDATE products SET attributes = attributes || '{"size": "XL"}' WHERE sku = 'T1'`);
        let row = (await c.query<{ size: string; a: string }>("SELECT size, attributes->>'size' AS a FROM products"))
          .rows[0];
        assert.deepEqual(row, { size: "XL", a: "XL" });
        await c.query("UPDATE products SET size = 'S' WHERE sku = 'T1'");
        row = (await c.query<{ size: string; a: string }>("SELECT size, attributes->>'size' AS a FROM products"))
          .rows[0];
        assert.deepEqual(row, { size: "S", a: "S" });

        const doc = (await c.query<{ d: Record<string, unknown> }>("SELECT product_document('T1') AS d")).rows[0]
          ?.d;
        assert.ok(doc);
        assert.deepEqual(Object.keys(doc).sort(), [
          "attributes",
          "brand",
          "category_levels",
          "category_path",
          "colour",
          "currency",
          "description_ar",
          "description_en",
          "price",
          "size",
          "sku",
          "title_ar",
          "title_en",
          "updated_at",
          "version",
        ]);
        assert.deepEqual(doc["category_levels"], [
          "electronics",
          "electronics.audio",
          "electronics.audio.headphones",
        ]);
        assert.equal(doc["category_path"], "electronics.audio.headphones");
        assert.equal(doc["size"], "S");
        assert.equal(doc["title_ar"], "سماعات سوني");
        assert.equal(doc["version"], await version());

        const missing = await c.query<{ d: unknown }>("SELECT product_document('nope') AS d");
        assert.equal(missing.rows[0]?.d, null);
      } finally {
        await c.query("ROLLBACK");
      }
    });
  });

  it("the contract migration moves size out of attributes without bumping versions", async () => {
    await withClient(testUrl, async (c) => {
      await c.query(`INSERT INTO products (sku, brand, category_path, price, currency, attributes)
                     VALUES ('C1', 'Acme', 'fashion.shoes', 10, 'AED', '{"size": "42", "material": "leather"}')`);
    });
    await runMigrations({ direction: "up", includeNext: true, databaseUrl: testUrl, log: quiet });
    await withClient(testUrl, async (c) => {
      const r = await c.query<{ size: string; attributes: Record<string, unknown>; version: string }>(
        "SELECT size, attributes, version FROM products WHERE sku = 'C1'",
      );
      assert.deepEqual(r.rows[0], { size: "42", attributes: { material: "leather" }, version: "1" });
      const d = await c.query<{ s: string }>("SELECT product_document('C1')->>'size' AS s");
      assert.equal(d.rows[0]?.s, "42");
    });
    await runMigrations({ direction: "down", includeNext: true, databaseUrl: testUrl, log: quiet });
    await withClient(testUrl, async (c) => {
      const r = await c.query<{ a: string }>("SELECT attributes->>'size' AS a FROM products WHERE sku = 'C1'");
      assert.equal(r.rows[0]?.a, "42", "contract down restores attributes.size");
    });
  });
});

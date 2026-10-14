import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { describe, it } from "node:test";
import { MIGRATIONS_DIR, NEXT_MIGRATIONS_DIR, migrationDirs, resolveDatabaseUrl } from "../../src/index.ts";

const NAME = /^(\d{13})_[a-z0-9_]+\.sql$/;

function sqlFiles(dir: string): string[] {
  return readdirSync(dir)
    .filter((f) => f.endsWith(".sql"))
    .sort();
}

function timestamp(file: string): number {
  const m = NAME.exec(file);
  assert.ok(m?.[1], `bad migration file name: ${file}`);
  return Number(m[1]);
}

describe("migration files", () => {
  const current = sqlFiles(MIGRATIONS_DIR);
  const next = sqlFiles(NEXT_MIGRATIONS_DIR);

  it("exist and follow <13-digit-ts>_<snake_name>.sql", () => {
    assert.ok(current.length > 0);
    for (const f of [...current, ...next]) assert.match(f, NAME);
  });

  it("every migration is reversible (has Up and Down sections, Up first)", () => {
    for (const [dir, files] of [
      [MIGRATIONS_DIR, current],
      [NEXT_MIGRATIONS_DIR, next],
    ] as const) {
      for (const f of files) {
        const sql = readFileSync(path.join(dir, f), "utf8");
        const up = sql.search(/^\s*--\s*Up Migration/im);
        const down = sql.search(/^\s*--\s*Down Migration/im);
        assert.ok(up >= 0 && down > up, `${f} needs "-- Up Migration" then "-- Down Migration"`);
      }
    }
  });

  it("contract migrations sort after every current-release migration", () => {
    // Otherwise moving a file from migrations-next/ into migrations/ next release would trip checkOrder.
    const lastCurrent = Math.max(...current.map(timestamp));
    for (const f of next) assert.ok(timestamp(f) > lastCurrent, `${f} must be newer than ${lastCurrent}`);
  });

  it("no current-release migration drops a column or table it did not create in its own Up", () => {
    // Expand/contract rule: drops belong in migrations-next/. A DROP in an Up section is only allowed for
    // objects that the same Up section re-creates (CREATE OR REPLACE is not a drop).
    for (const f of current) {
      const sql = readFileSync(path.join(MIGRATIONS_DIR, f), "utf8");
      const upSection = sql.slice(0, sql.search(/^\s*--\s*Down Migration/im));
      assert.doesNotMatch(upSection, /\bDROP\s+(TABLE|COLUMN)\b/i, `${f} drops in its Up section`);
    }
  });

  it("includes migrations-next only when asked", () => {
    assert.deepEqual(migrationDirs(false), [MIGRATIONS_DIR]);
    assert.deepEqual(migrationDirs(true), [MIGRATIONS_DIR, NEXT_MIGRATIONS_DIR]);
  });
});

describe("resolveDatabaseUrl", () => {
  it("defaults to the compose Postgres on 55432", () => {
    assert.equal(resolveDatabaseUrl({}), "postgres://souq:souq@localhost:55432/souq");
    assert.equal(resolveDatabaseUrl({ DATABASE_URL: "" }), "postgres://souq:souq@localhost:55432/souq");
  });
  it("honours DATABASE_URL", () => {
    assert.equal(resolveDatabaseUrl({ DATABASE_URL: "postgres://x@h:1/d" }), "postgres://x@h:1/d");
  });
});

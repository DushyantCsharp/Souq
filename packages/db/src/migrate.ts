// CLI: node src/migrate.ts <up|down|redo> [count|all] [--include-next] [--dry-run]
// DATABASE_URL defaults to the compose Postgres on localhost:55432.
import { resolveDatabaseUrl, runMigrations, type Direction } from "./index.ts";

function parseCount(raw: string | undefined): number | undefined {
  if (raw === undefined) return undefined;
  if (raw === "all") return Infinity;
  const n = Number.parseInt(raw, 10);
  if (!Number.isFinite(n) || n <= 0 || String(n) !== raw) {
    throw new Error(`count must be a positive integer or "all", got "${raw}"`);
  }
  return n;
}

async function main(argv: string[]): Promise<void> {
  const flags = new Set(argv.filter((a) => a.startsWith("--")));
  const positional = argv.filter((a) => !a.startsWith("--"));
  const command = positional[0] ?? "up";
  const count = parseCount(positional[1]);
  const includeNext = flags.has("--include-next");
  const dryRun = flags.has("--dry-run");
  const databaseUrl = resolveDatabaseUrl();
  const base = { includeNext, dryRun, databaseUrl };

  const run = (direction: Direction, n: number | undefined) =>
    runMigrations(n === undefined ? { ...base, direction } : { ...base, direction, count: n });

  switch (command) {
    case "up":
    case "down":
      await run(command, count);
      return;
    case "redo": {
      // Reverts then re-applies the same N (default 1): the quickest check that a down section is correct.
      const n = count ?? 1;
      await run("down", n);
      await run("up", n);
      return;
    }
    default:
      throw new Error(`unknown command "${command}" (expected up | down | redo)`);
  }
}

main(process.argv.slice(2)).catch((err: unknown) => {
  console.error(err instanceof Error ? err.message : err);
  process.exitCode = 1;
});

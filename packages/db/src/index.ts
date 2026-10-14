import path from "node:path";
import { fileURLToPath } from "node:url";
import { runner, type RunnerOption } from "node-pg-migrate";

const packageDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

/** Migrations for the current release (expand steps). */
export const MIGRATIONS_DIR = path.join(packageDir, "migrations");
/**
 * Contract steps staged for the next release. Kept out of MIGRATIONS_DIR so a routine `migrate up` can never
 * drop something the running release still reads.
 */
export const NEXT_MIGRATIONS_DIR = path.join(packageDir, "migrations-next");
export const MIGRATIONS_TABLE = "pgmigrations";
export const DEFAULT_DATABASE_URL = "postgres://souq:souq@localhost:55432/souq";

export type Direction = "up" | "down";

export interface MigrateOptions {
  direction: Direction;
  /** Number of migrations to apply/revert. Defaults: all for up, 1 for down (node-pg-migrate semantics). */
  count?: number;
  /** Also consider migrations-next/ (the next release's contract migrations). */
  includeNext?: boolean;
  databaseUrl?: string;
  dryRun?: boolean;
  log?: (msg: string) => void;
}

export function resolveDatabaseUrl(env: NodeJS.ProcessEnv = process.env): string {
  const url = env["DATABASE_URL"];
  return url !== undefined && url !== "" ? url : DEFAULT_DATABASE_URL;
}

export function migrationDirs(includeNext: boolean): string[] {
  return includeNext ? [MIGRATIONS_DIR, NEXT_MIGRATIONS_DIR] : [MIGRATIONS_DIR];
}

export async function runMigrations(opts: MigrateOptions): Promise<string[]> {
  const dirs = migrationDirs(opts.includeNext ?? false);
  const options: RunnerOption = {
    databaseUrl: opts.databaseUrl ?? resolveDatabaseUrl(),
    // Globbing lets one run span both directories while sharing one pgmigrations table, so the order check
    // still sees the full history once a contract migration has been applied.
    dir: dirs.map((d) => path.join(d, "*.sql")),
    useGlob: true,
    migrationsTable: MIGRATIONS_TABLE,
    direction: opts.direction,
    count: opts.count ?? (opts.direction === "up" ? Infinity : 1),
    checkOrder: true,
    singleTransaction: true,
    dryRun: opts.dryRun ?? false,
    // Two agents/CI jobs migrating the same database should queue, not fail.
    advisoryLockMode: "wait",
    log: opts.log ?? ((msg: string) => process.stdout.write(`${msg}\n`)),
  };
  const ran = await runner(options);
  return ran.map((m) => m.name);
}

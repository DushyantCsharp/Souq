// Drift check: regenerate every artefact into a scratch directory and compare it with what is committed.
// Exit 1 (with a diff) when anything differs, is missing, or is extra. CI runs this; locally:
//   pnpm --filter @souq/contracts drift-check
import { spawnSync } from "node:child_process";
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { codegen, OUTPUTS, REPO_ROOT } from "./codegen.ts";

// Build/run by-products that live next to generated files but are not generator output.
const IGNORED = new Set(["__pycache__", ".DS_Store", ".mypy_cache", ".ruff_cache"]);

function listFiles(root: string, rel = ""): string[] {
  const dir = join(root, rel);
  if (!existsSync(dir)) return [];
  if (statSync(dir).isFile()) return [rel];
  return readdirSync(dir)
    .filter((name) => !IGNORED.has(name) && !name.endsWith(".pyc"))
    .flatMap((name) => listFiles(root, rel ? join(rel, name) : name));
}

function showDiff(expected: string, actual: string): void {
  // Best effort: `diff` exists on every CI image we use; the file list below is authoritative anyway.
  const result = spawnSync("diff", ["-u", actual, expected], { encoding: "utf8" });
  if (result.stdout) process.stderr.write(result.stdout.split("\n").slice(0, 80).join("\n") + "\n");
}

const scratch = await mkdtemp(join(tmpdir(), "souq-contracts-drift-"));
const drifted: string[] = [];
try {
  await codegen(scratch);
  for (const target of Object.values(OUTPUTS)) {
    const committed = new Set(listFiles(REPO_ROOT, target));
    const fresh = new Set(listFiles(scratch, target));
    for (const file of new Set([...committed, ...fresh])) {
      if (!fresh.has(file)) {
        drifted.push(`${file} (committed but no longer generated)`);
      } else if (!committed.has(file)) {
        drifted.push(`${file} (generated but not committed)`);
      } else if (!readFileSync(join(REPO_ROOT, file)).equals(readFileSync(join(scratch, file)))) {
        drifted.push(file);
        showDiff(join(scratch, file), join(REPO_ROOT, file));
      }
    }
  }
} finally {
  await rm(scratch, { recursive: true, force: true });
}

if (drifted.length > 0) {
  console.error(`\ncontracts drift: ${drifted.length} generated file(s) are stale:`);
  for (const file of drifted) console.error(`  - ${file}`);
  console.error("\nRun `pnpm --filter @souq/contracts codegen` and commit the result.");
  process.exit(1);
}
console.warn(`contracts: no drift across ${Object.keys(OUTPUTS).length} generated targets`);

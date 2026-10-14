// ts-jest in ESM mode; `isolatedModules` in tsconfig makes it transpile-only (typecheck is its own task).
/** @type {import('jest').Config} */
export default {
  testEnvironment: "node",
  testMatch: ["<rootDir>/test/**/*.test.ts"],
  extensionsToTreatAsEsm: [".ts"],
  // Sources import siblings as `./x.js` (NodeNext); map back to the .ts file for Jest.
  moduleNameMapper: { "^(\\.{1,2}/.*)\\.js$": "$1" },
  transform: { "^.+\\.ts$": ["ts-jest", { useESM: true, tsconfig: "tsconfig.json" }] },
  testTimeout: 15000,
  // `@live` tests call real external services and only run with JEST_INCLUDE_LIVE=1 (CLAUDE.md rule 7).
  testNamePattern: process.env.JEST_INCLUDE_LIVE === "1" ? undefined : "^(?!.*@live)",
};

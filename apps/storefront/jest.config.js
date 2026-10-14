// next/jest wires SWC (same transforms as the app), CSS/font/image mocks and next.config loading.
// Tests: apps/storefront/__tests__/** (tests agent) and colocated src/**/*.test.ts(x) (unit tests).
import nextJest from "next/jest.js";

const createJestConfig = nextJest({ dir: import.meta.dirname });

/** @type {import("jest").Config} */
const config = {
  testEnvironment: "jsdom",
  setupFilesAfterEnv: ["<rootDir>/jest.setup.ts"],
  moduleNameMapper: { "^@/(.*)$": "<rootDir>/src/$1" },
  testPathIgnorePatterns: ["/node_modules/", "<rootDir>/.next/"],
  // .next/standalone contains a copy of this package.json; without this, haste reports a name collision.
  modulePathIgnorePatterns: ["<rootDir>/.next/"],
  // Live LLM tests are tagged @live in their name and never run in default CI (CLAUDE.md rule 7).
  testNamePattern: process.env.JEST_INCLUDE_LIVE === "1" ? undefined : "^(?!.*@live)",
};

export default createJestConfig(config);

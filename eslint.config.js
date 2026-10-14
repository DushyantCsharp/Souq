// Root ESLint flat config, shared by every TS package. ESLint resolves the nearest eslint.config.* upward,
// so packages without their own config use this one; packages that need extra plugins (the storefront)
// import this array and append to it rather than redefining the base.
//
// Deliberately not type-aware: `tsc --noEmit` already runs as its own turbo task, and type-aware linting
// would fail on any file outside a tsconfig `include`, which is fragile while packages are being built.
import js from "@eslint/js";
import { defineConfig } from "eslint/config";
import globals from "globals";
import tseslint from "typescript-eslint";

export const ignores = {
  ignores: [
    "**/node_modules/**",
    "**/dist/**",
    "**/build/**",
    "**/coverage/**",
    "**/.next/**",
    "**/.turbo/**",
    "**/.venv/**",
    "**/next-env.d.ts",
    // Generated code: GraphQL/OpenAPI codegen output is checked by its generator, not hand-linted.
    "**/src/gql/**",
    "**/src/generated/**",
    "**/generated/**",
    "libs/admin-sdk/**",
    "models/artifacts/**",
    "loadtest/results/**",
  ],
};

export default defineConfig(
  ignores,
  js.configs.recommended,
  tseslint.configs.recommended,
  {
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: "module",
      globals: { ...globals.node },
    },
    linterOptions: { reportUnusedDisableDirectives: "error" },
    rules: {
      "@typescript-eslint/consistent-type-imports": ["error", { fixStyle: "inline-type-imports" }],
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_", caughtErrorsIgnorePattern: "^_" },
      ],
      "@typescript-eslint/no-explicit-any": "error",
      eqeqeq: ["error", "smart"],
      "no-console": ["warn", { allow: ["warn", "error"] }],
    },
  },
  {
    // k6 scripts run in k6's goja runtime, not Node.
    files: ["loadtest/**/*.js"],
    languageOptions: { globals: { __ENV: "readonly", __VU: "readonly", __ITER: "readonly", open: "readonly" } },
    rules: { "no-console": "off" },
  },
  {
    files: ["**/*.test.ts", "**/*.test.tsx", "**/test/**/*.ts", "**/__tests__/**/*.{ts,tsx}"],
    languageOptions: { globals: { ...globals.jest } },
    rules: { "no-console": "off" },
  },
);

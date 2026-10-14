// Extends the root config (TS rules, ignores) with the Next.js and React Hooks rule sets for this app only.
import nextPlugin from "@next/eslint-plugin-next";
import reactHooks from "eslint-plugin-react-hooks";
import { defineConfig } from "eslint/config";
import root from "../../eslint.config.js";

export default defineConfig(
  root,
  { files: ["**/*.{ts,tsx,js}"], extends: [nextPlugin.configs["core-web-vitals"]] },
  { files: ["**/*.{ts,tsx}"], extends: [reactHooks.configs.flat.recommended] },
);

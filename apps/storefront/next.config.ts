import { fileURLToPath } from "node:url";
import type { NextConfig } from "next";

// The monorepo root: lets standalone output trace workspace deps and keeps Turbopack from guessing the root
// from whichever lockfile it finds first.
const repoRoot = fileURLToPath(new URL("../..", import.meta.url));

const nextConfig: NextConfig = {
  output: "standalone",
  outputFileTracingRoot: repoRoot,
  turbopack: { root: repoRoot },
  // `next build` type-checks app code only; tests are type-checked by the `typecheck` task (tsconfig.json),
  // so a broken test fixture fails typecheck/test rather than blocking a deployable build.
  typescript: { tsconfigPath: "tsconfig.build.json" },
  poweredByHeader: false,
  reactStrictMode: true,
  async redirects() {
    return [
      // Arabic-first browsers land on /ar; everyone else on /en. The locale is then explicit in the URL.
      {
        source: "/",
        has: [{ type: "header", key: "accept-language", value: "ar.*" }],
        destination: "/ar",
        permanent: false,
      },
      { source: "/", destination: "/en", permanent: false },
    ];
  },
};

export default nextConfig;

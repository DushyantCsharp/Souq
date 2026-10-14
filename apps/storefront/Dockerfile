# syntax=docker/dockerfile:1
# @souq/storefront: Next.js App Router, served from `output: "standalone"`. Build context is the repo root:
#   docker buildx bake storefront   (or)   docker build -f apps/storefront/Dockerfile .
# apps/storefront/Dockerfile.dockerignore whitelists what this image needs.

ARG NODE_VERSION=24

FROM node:${NODE_VERSION}-alpine AS base
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    CI=1 \
    NEXT_TELEMETRY_DISABLED=1
RUN corepack enable
WORKDIR /repo

# Built on the target platform (not $BUILDPLATFORM): standalone output traces native packages such as
# sharp's musl binaries, which must match the runtime architecture.
FROM base AS build
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN --mount=type=cache,id=souq-pnpm-store,target=/pnpm/store \
    pnpm fetch --frozen-lockfile --store-dir /pnpm/store
COPY . .
RUN --mount=type=cache,id=souq-pnpm-store,target=/pnpm/store \
    pnpm install --frozen-lockfile --offline --store-dir /pnpm/store --filter "@souq/storefront..."
RUN --mount=type=cache,id=souq-next-cache,target=/repo/apps/storefront/.next/cache \
    pnpm --filter @souq/storefront run build \
 && mkdir -p apps/storefront/public

FROM node:${NODE_VERSION}-alpine AS runtime
RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack \
           /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack \
           /opt/yarn-* /usr/local/bin/yarn /usr/local/bin/yarnpkg
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000 \
    SOUQ_CONFIG_DIR=/app/config
WORKDIR /app
# Explicit rather than relying on output tracing: budgets.yaml holds the storefront's gateway timeout.
COPY config ./config
# outputFileTracingRoot is the repo root, so the standalone tree mirrors the monorepo layout.
COPY --from=build /repo/apps/storefront/.next/standalone ./
COPY --from=build /repo/apps/storefront/.next/static ./apps/storefront/.next/static
COPY --from=build /repo/apps/storefront/public ./apps/storefront/public
USER 1000:1000
EXPOSE 3000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:' + (process.env.PORT || 3000) + '/api/healthz', { signal: AbortSignal.timeout(2000) }).then((r) => process.exit(r.ok ? 0 : 1), () => process.exit(1))"]
CMD ["node", "apps/storefront/server.js"]

# syntax=docker/dockerfile:1
# @souq/gateway: Fastify REST /v1 + GraphQL /graphql. Build context is the repo root:
#   docker buildx bake gateway   (or)   docker build -f apps/gateway/Dockerfile .
# apps/gateway/Dockerfile.dockerignore whitelists what this image needs (BuildKit prefers it over the root
# .dockerignore). Generated contract types are committed, so no codegen (which needs Python) runs here.

ARG NODE_VERSION=24

FROM node:${NODE_VERSION}-alpine AS base
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    CI=1
# corepack pins pnpm to the root package.json "packageManager" version.
RUN corepack enable
WORKDIR /repo

FROM base AS build
# Lockfile-only fetch: the store layer survives source edits and is reused across the Node images.
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN --mount=type=cache,id=souq-pnpm-store,target=/pnpm/store \
    pnpm fetch --frozen-lockfile --store-dir /pnpm/store
COPY . .
RUN --mount=type=cache,id=souq-pnpm-store,target=/pnpm/store \
    pnpm install --frozen-lockfile --offline --store-dir /pnpm/store --filter "@souq/gateway..."
# --legacy: pnpm 10 only allows non-legacy deploy with inject-workspace-packages, which would change how
# every workspace links its deps; the gateway has no runtime workspace deps, so legacy deploy is exact.
RUN --mount=type=cache,id=souq-pnpm-store,target=/pnpm/store \
    pnpm --filter @souq/gateway run build \
 && pnpm --filter @souq/gateway deploy --prod --legacy --store-dir /pnpm/store /out

FROM node:${NODE_VERSION}-alpine AS runtime
# The runtime only executes `node`; package managers are dead weight and a recurring CVE source.
RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack \
           /usr/local/bin/npm /usr/local/bin/npx /usr/local/bin/corepack \
           /opt/yarn-* /usr/local/bin/yarn /usr/local/bin/yarnpkg
ENV NODE_ENV=production \
    HOST=0.0.0.0 \
    PORT=4000 \
    SOUQ_CONFIG_DIR=/app/config
WORKDIR /app
# Dependencies before code: node_modules only changes with the lockfile, dist with every commit.
COPY --from=build /out/package.json ./package.json
COPY --from=build /out/node_modules ./node_modules
COPY config ./config
COPY --from=build /repo/apps/gateway/dist ./dist
# uid 1000 is the image's built-in unprivileged `node` user.
USER 1000:1000
EXPOSE 4000
HEALTHCHECK --interval=15s --timeout=3s --start-period=15s --retries=3 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:' + (process.env.PORT || 4000) + '/healthz', { signal: AbortSignal.timeout(2000) }).then((r) => process.exit(r.ok ? 0 : 1), () => process.exit(1))"]
CMD ["node", "--import", "./dist/telemetry.js", "dist/server.js"]

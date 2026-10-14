# @souq/contracts

Source of truth for every API boundary in Souq, plus the generators that turn the contracts into typed
clients. Hand-edit only the inputs; everything under "Outputs" is generated and drift-checked.

## Inputs

| File | What |
|---|---|
| `openapi.yaml` | Public gateway REST API (OpenAPI 3.1): `GET /v1/search`, `POST /v1/feeds`, `GET /v1/feeds/{id}`, `GET /v1/feeds/{id}/errors`, `/healthz`, `/readyz` |
| `schema.graphql` | Storefront GraphQL schema served by the gateway at `/graphql` |
| `search-internal/openapi.json` | services/search spec, **exported from FastAPI by the search service** (not hand-edited here) |
| `search-internal/openapi.provisional.json` | Hand-written stand-in from CLAUDE.md, used only while `openapi.json` is absent |
| `feeds-internal/openapi.json` / `openapi.provisional.json` | Same pattern for services/feeds |
| `admin-sdk.config.yaml`, `admin-sdk-templates/` | openapi-python-client config and the one template override (deepObject query params) |

Codegen always prefers `<service>/openapi.json` and falls back to `openapi.provisional.json`; the banner
line of each generated `.d.ts` records which one was used.

## Outputs

| Path | Generator | Used by |
|---|---|---|
| `generated/public-api.d.ts` | openapi-typescript | gateway response types (`@souq/contracts/public-api`) |
| `generated/search-internal.d.ts` | openapi-typescript | gateway → search client (openapi-fetch) |
| `generated/feeds-internal.d.ts` | openapi-typescript | gateway → feeds client (openapi-fetch) |
| `apps/gateway/src/generated/graphql.ts` | graphql-codegen `typescript` + `typescript-resolvers` | typed resolvers |
| `apps/gateway/src/generated/schema.ts` | codegen.ts | `typeDefs` string compiled into the gateway |
| `apps/storefront/src/gql/` | graphql-codegen client preset (`documentMode: "string"`, no fragment masking) from `apps/storefront/src/graphql/**/*.graphql` | storefront |
| `libs/admin-sdk/src/souq_admin_sdk/` | openapi-python-client 0.29.1 (pinned) + ruff | Python admin SDK (`souq-admin-sdk`) |

The `.d.ts` exports are type-only: nothing imports `@souq/contracts` at runtime, so it is a
devDependency of the gateway and is not needed in production images.

## Commands

```bash
pnpm --filter @souq/contracts codegen      # regenerate every output above (needs Node + uv)
pnpm --filter @souq/contracts drift-check  # regenerate into a temp dir, diff against committed; exit 1 on drift
pnpm --filter @souq/contracts lint         # eslint + Redocly lint of all specs + GraphQL schema/doc validation
pnpm --filter @souq/contracts test         # cross-contract consistency tests (node:test)
pnpm turbo codegen                         # same as codegen, wired through turbo (^codegen ordering)
```

`check-drift` is an alias of `drift-check`. The drift check needs Node 24 and `uv` (it runs
`uvx openapi-python-client` and the workspace ruff via `uv run --no-sync`, so `uv sync` must have run).
CI fails the build when it exits non-zero; fix by running `codegen` and committing the result.

## Design decisions

### Filters in the query string (REST)
`filters` is an OpenAPI `style: deepObject, explode: true` parameter whose keys are exactly the internal
`Filters` names, which are also the names used in `applied_filters`:

```
GET /v1/search?q=running+shoes&filters[brands]=nike&filters[brands]=adidas&filters[price_max]=300
```

Multi-valued fields (`brands`, `colours`, `sizes`) repeat the key (OR within a field, AND across fields);
values are never comma-split, so brand names may contain commas. Unknown filter names, a bare `filters=`,
bad numbers or `price_min > price_max` are `400 validation-error` with per-parameter `errors[]`. The
generated Python SDK serialises `Filters` this way thanks to `admin-sdk-templates/endpoint_macros.py.jinja`
(upstream openapi-python-client would flatten the object into top-level params).

### Page tokens
`next_page_token` = `base64url(JSON{c: Cursor, qh: sha256(q|lang|filters canonical), exp}) + "." +
base64url(HMAC-SHA256(PAGE_TOKEN_SECRET, first segment))`. The cursor (PIT id, `search_after`, rerank
window position) is produced by services/search and stays opaque to the gateway. `exp` is the PIT
keep-alive (`search.pit_keep_alive` in `config/budgets.yaml`). Tampered, expired, query-mismatched or
stale (snapshot gone upstream) tokens are `400 https://souq.dev/problems/invalid-page-token`. GraphQL uses
the same token as the Relay cursor; pagination is page-granular, so every edge carries `pageInfo.endCursor`.

### Errors
Every non-2xx is `application/problem+json` (RFC 9457) with `type = https://souq.dev/problems/<slug>`:
`validation-error`, `invalid-page-token` (400), `unauthorized` (401), `not-found` (404),
`payload-too-large` (413), `unsupported-media-type` (415), `rate-limited` (429 + `Retry-After`),
`internal` (500), `bad-gateway` (502), `search-unavailable` / `feeds-unavailable` (503),
`upstream-timeout` (504). GraphQL reports the same taxonomy as `extensions.code`
(`BAD_USER_INPUT`, `INVALID_PAGE_TOKEN`, `UPSTREAM_TIMEOUT`, `SEARCH_UNAVAILABLE`) plus `extensions.type`.

### Auth
Feed endpoints require `X-API-Key` (one of `GATEWAY_API_KEYS`); search and health are public. Rate limits
are per valid API key, else per client IP.

## Updating a contract

1. Edit `openapi.yaml` / `schema.graphql` (or re-export a service spec into `<service>/openapi.json`).
2. `pnpm --filter @souq/contracts codegen && pnpm --filter @souq/contracts lint test`.
3. Fix the gateway/storefront type errors the regenerated types surface; commit inputs and outputs together.

To bump openapi-python-client, change `OPENAPI_PYTHON_CLIENT` in `scripts/codegen.ts`, re-copy
`endpoint_macros.py.jinja` from the new version and re-apply the marked `query_params` override.

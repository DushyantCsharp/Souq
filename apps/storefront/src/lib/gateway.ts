// Server-only GraphQL client for the gateway. The storefront never calls search or feeds directly, and the
// browser never calls the gateway: GATEWAY_URL is an in-cluster address (http://gateway:4000 in compose).
import type { DocumentTypeDecoration } from "@graphql-typed-document-node/core";
import { type DocumentNode, print } from "graphql";
import { gatewayTimeoutMs } from "./budgets";

export const DEFAULT_GATEWAY_URL = "http://localhost:4000/graphql";

// GraphQL errors arrive with HTTP 200, so the gateway's extensions.code says whether search itself failed.
const UNAVAILABLE_CODES = new Set(["UPSTREAM_TIMEOUT", "SEARCH_UNAVAILABLE"]);

export type GatewayErrorKind = "unavailable" | "invalid_page_token" | "bad_response";

export class GatewayError extends Error {
  constructor(
    message: string,
    readonly kind: GatewayErrorKind,
    readonly status?: number,
  ) {
    super(message);
    this.name = "GatewayError";
  }
}

interface GraphQLErrorShape {
  message: string;
  extensions?: { code?: unknown; type?: unknown; status?: unknown } & Record<string, unknown>;
}

interface GraphQLResponse<T> {
  data?: T | null;
  errors?: GraphQLErrorShape[];
}

/** Accepts codegen output in either documentMode: TypedDocumentNode (AST) or TypedDocumentString. */
export function documentText<TResult, TVariables>(doc: DocumentTypeDecoration<TResult, TVariables>): string {
  if (typeof doc === "string" || doc instanceof String) return doc.toString();
  return print(doc as DocumentNode);
}

/** Expired, tampered or query-mismatched page tokens; the gateway reports them as invalid-page-token. */
export function isInvalidPageTokenError(error: GraphQLErrorShape): boolean {
  const { code, type } = error.extensions ?? {};
  return (
    code === "INVALID_PAGE_TOKEN" ||
    (typeof type === "string" && type.endsWith("/invalid-page-token")) ||
    /invalid[- ]page[- ]token/i.test(error.message)
  );
}

export async function gatewayQuery<TResult, TVariables>(
  document: DocumentTypeDecoration<TResult, TVariables>,
  variables: TVariables,
  init: { url?: string; fetchImpl?: typeof fetch; timeoutMs?: number } = {},
): Promise<TResult> {
  const url = init.url ?? process.env.GATEWAY_URL ?? DEFAULT_GATEWAY_URL;
  const doFetch = init.fetchImpl ?? fetch;
  let res: Response;
  try {
    res = await doFetch(url, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        accept: "application/graphql-response+json, application/json",
      },
      body: JSON.stringify({ query: documentText(document), variables }),
      cache: "no-store",
      signal: AbortSignal.timeout(init.timeoutMs ?? gatewayTimeoutMs()),
    });
  } catch (cause) {
    throw new GatewayError(`gateway request failed: ${String(cause)}`, "unavailable");
  }

  let body: GraphQLResponse<TResult>;
  try {
    body = (await res.json()) as GraphQLResponse<TResult>;
  } catch {
    throw new GatewayError(`gateway returned non-JSON (HTTP ${res.status})`, "bad_response", res.status);
  }

  const errors = body.errors ?? [];
  if (errors.some(isInvalidPageTokenError)) {
    throw new GatewayError("page token rejected", "invalid_page_token", res.status);
  }
  if (errors.length > 0 || body.data == null) {
    const detail = errors.map((e) => e.message).join("; ") || `HTTP ${res.status} without data`;
    const upstream = res.status >= 500 || errors.some((e) => UNAVAILABLE_CODES.has(String(e.extensions?.code)));
    throw new GatewayError(`gateway error: ${detail}`, upstream ? "unavailable" : "bad_response", res.status);
  }
  return body.data;
}

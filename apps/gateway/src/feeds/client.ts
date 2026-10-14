// Client for services/feeds. JSON endpoints use openapi-fetch over the generated spec; the upload is a
// raw streaming pass-through because openapi-fetch would need the multipart body materialised.
import { Readable, Transform, type TransformCallback } from "node:stream";

import type { components as PublicComponents } from "@souq/contracts/public-api";
import type { paths } from "@souq/contracts/feeds-internal";
import createClient from "openapi-fetch";

import {
  budgetSignal,
  classifyFetchFailure,
  classifyStatus,
  describeErrorBody,
  UpstreamError,
} from "../errors/upstream.js";

type PublicSchemas = PublicComponents["schemas"];
export type FeedAccepted = PublicSchemas["FeedAccepted"];
export type FeedStatus = PublicSchemas["FeedStatus"];
export type FeedErrorPage = PublicSchemas["FeedErrorPage"];

type UploadResponse = paths["/feeds"]["post"]["responses"][202]["content"]["application/json"];
type StatusResponse = paths["/feeds/{feed_id}"]["get"]["responses"][200]["content"]["application/json"];
type ErrorsResponse = paths["/feeds/{feed_id}/errors"]["get"]["responses"][200]["content"]["application/json"];

export interface FeedsCallOptions {
  requestId?: string;
  signal?: AbortSignal;
}

export interface FeedUpload {
  body: Readable;
  contentType: string;
  /** Forwarded when the client declared one, so the feeds service sees a fixed-length body. */
  contentLength?: number;
  /** Hard cap enforced while streaming (clients can lie about or omit content-length). */
  maxBytes: number;
}

export interface FeedsClient {
  upload(upload: FeedUpload, options?: FeedsCallOptions): Promise<FeedAccepted>;
  get(id: string, options?: FeedsCallOptions): Promise<FeedStatus>;
  errors(id: string, page: { cursor?: string; limit: number }, options?: FeedsCallOptions): Promise<FeedErrorPage>;
}

export interface FeedsClientOptions {
  baseUrl: string;
  /** `gateway.feeds_timeout_ms`: idle timeout while streaming, then the wait for the response. */
  timeoutMs: number;
  fetch?: typeof globalThis.fetch;
}

export class UploadTooLargeError extends Error {
  constructor(readonly maxBytes: number) {
    super(`upload exceeds ${maxBytes} bytes`);
    this.name = "UploadTooLargeError";
  }
}

/** Counts bytes and calls `onProgress` per chunk; errors once `maxBytes` is exceeded. */
class ByteLimit extends Transform {
  private seen = 0;
  exceeded = false;

  constructor(
    private readonly maxBytes: number,
    private readonly onProgress: () => void,
  ) {
    super();
  }

  override _transform(chunk: Buffer, _enc: BufferEncoding, done: TransformCallback): void {
    this.seen += chunk.length;
    if (this.seen > this.maxBytes) {
      this.exceeded = true;
      done(new UploadTooLargeError(this.maxBytes));
      return;
    }
    this.onProgress();
    done(null, chunk);
  }
}

function toFeedStatus(data: StatusResponse): FeedStatus {
  return {
    id: String(data.id),
    status: data.status,
    rows_total: data.rows_total,
    rows_ok: data.rows_ok,
    rows_rejected: data.rows_rejected,
    error_counts: { ...(data.error_counts ?? {}) },
    started_at: data.started_at ?? null,
    finished_at: data.finished_at ?? null,
    failure_reason: data.failure_reason ?? null,
  };
}

function toErrorPage(data: ErrorsResponse): FeedErrorPage {
  return {
    items: data.items.map((row) => ({
      row_number: row.row_number,
      code: row.code as FeedErrorPage["items"][number]["code"],
      field: row.field ?? null,
      message: row.message,
      raw: row.raw ?? null,
    })),
    next_page_token: data.next_cursor ?? null,
  };
}

function requestHeaders(options: FeedsCallOptions): Record<string, string> {
  return options.requestId ? { "x-request-id": options.requestId } : {};
}

export function createFeedsClient(options: FeedsClientOptions): FeedsClient {
  const doFetch = options.fetch ?? globalThis.fetch;
  const client = createClient<paths>({ baseUrl: options.baseUrl, fetch: doFetch });

  async function upload(upload: FeedUpload, callOptions: FeedsCallOptions = {}): Promise<FeedAccepted> {
    // A fixed AbortSignal.timeout would kill large-but-healthy uploads, so the budget is an idle timer
    // re-armed on every chunk and once more when the body is complete (waiting for the 202).
    const controller = new AbortController();
    let timer: NodeJS.Timeout | undefined;
    const arm = (): void => {
      clearTimeout(timer);
      timer = setTimeout(
        () => controller.abort(new DOMException("feeds upload idle timeout", "TimeoutError")),
        options.timeoutMs,
      );
    };
    const limiter = new ByteLimit(upload.maxBytes, arm);
    limiter.on("end", arm);
    upload.body.on("error", (err) => limiter.destroy(err));
    upload.body.pipe(limiter);
    const signal = callOptions.signal ? AbortSignal.any([controller.signal, callOptions.signal]) : controller.signal;

    const headers: Record<string, string> = { "content-type": upload.contentType, ...requestHeaders(callOptions) };
    if (upload.contentLength !== undefined) headers["content-length"] = String(upload.contentLength);

    arm();
    let response: Response;
    try {
      response = await doFetch(new URL("/feeds", options.baseUrl), {
        method: "POST",
        headers,
        body: Readable.toWeb(limiter) as ReadableStream<Uint8Array>,
        duplex: "half",
        signal,
      } as RequestInit);
    } catch (err) {
      if (limiter.exceeded) throw new UpstreamError("feeds", "too_large", `upload exceeds ${upload.maxBytes} bytes`);
      throw classifyFetchFailure("feeds", signal.aborted ? signal.reason : err);
    } finally {
      clearTimeout(timer);
    }

    const body: unknown = await response.json().catch(() => undefined);
    if (!response.ok) throw classifyStatus("feeds", response.status, describeErrorBody(body));
    const accepted = body as Partial<UploadResponse> | undefined;
    if (!accepted || accepted.id === undefined || accepted.status === undefined) {
      throw new UpstreamError("feeds", "bad_response", "feeds service returned an unexpected body");
    }
    return { id: String(accepted.id), status: accepted.status };
  }

  async function get(id: string, callOptions: FeedsCallOptions = {}): Promise<FeedStatus> {
    let result;
    try {
      result = await client.GET("/feeds/{feed_id}", {
        params: { path: { feed_id: id } },
        headers: requestHeaders(callOptions),
        signal: budgetSignal(options.timeoutMs, callOptions.signal),
      });
    } catch (err) {
      throw classifyFetchFailure("feeds", err);
    }
    if (!result.response.ok || result.data === undefined) {
      throw classifyStatus("feeds", result.response.status, describeErrorBody(result.error));
    }
    return toFeedStatus(result.data);
  }

  async function errors(
    id: string,
    page: { cursor?: string; limit: number },
    callOptions: FeedsCallOptions = {},
  ): Promise<FeedErrorPage> {
    let result;
    try {
      result = await client.GET("/feeds/{feed_id}/errors", {
        params: {
          path: { feed_id: id },
          query: { limit: page.limit, ...(page.cursor ? { cursor: page.cursor } : {}) },
        },
        headers: requestHeaders(callOptions),
        signal: budgetSignal(options.timeoutMs, callOptions.signal),
      });
    } catch (err) {
      throw classifyFetchFailure("feeds", err);
    }
    if (!result.response.ok || result.data === undefined) {
      throw classifyStatus("feeds", result.response.status, describeErrorBody(result.error));
    }
    return toErrorPage(result.data);
  }

  return { upload, get, errors };
}

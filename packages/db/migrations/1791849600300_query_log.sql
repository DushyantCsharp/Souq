-- Up Migration

-- One row per search request, written by services/search (best effort, off the request path).
CREATE TABLE query_log (
  id              bigserial PRIMARY KEY,
  -- souq_common.telemetry.hash_query(normalised text): groups identical queries without reading the text.
  query_hash      text NOT NULL,
  -- Normalised query text. Nullable so a deployment can log hashes only (query text can be PII).
  query_text      text,
  lang            text NOT NULL CHECK (lang IN ('en', 'ar', 'auto')),
  detected_script text CHECK (detected_script IN ('ar', 'en', 'mixed', 'arabizi')),
  parser_path     text NOT NULL CHECK (parser_path IN ('rule', 'llm', 'cache')),
  -- Per-stage latency in ms, keys as in the response timings_ms: {"normalise": 0.3, "bm25": 12.1, ...}.
  latency_ms      jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(latency_ms) = 'object'),
  degraded        text[] NOT NULL DEFAULT '{}',
  result_count    integer NOT NULL CHECK (result_count >= 0),
  created_at      timestamptz NOT NULL DEFAULT now()
);

-- Append-only and time-ordered: BRIN is tiny and serves time-range scans for dashboards and retention.
CREATE INDEX query_log_created_at_brin ON query_log USING brin (created_at);
CREATE INDEX query_log_hash_idx ON query_log (query_hash, created_at);
-- Zero-result query review is the main analyst workflow.
CREATE INDEX query_log_zero_result_idx ON query_log (created_at) WHERE result_count = 0;

-- Down Migration

DROP TABLE IF EXISTS query_log;

-- Up Migration

CREATE TABLE feed_imports (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  filename       text,
  format         text NOT NULL CHECK (format IN ('csv', 'jsonl')),
  status         text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'completed', 'failed')),
  rows_total     integer NOT NULL DEFAULT 0 CHECK (rows_total >= 0),
  rows_ok        integer NOT NULL DEFAULT 0 CHECK (rows_ok >= 0),
  rows_rejected  integer NOT NULL DEFAULT 0 CHECK (rows_rejected >= 0),
  -- {"<reject code>": n}; denormalised so GET /feeds/{id} does not aggregate feed_row_errors per poll.
  error_counts   jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(error_counts) = 'object'),
  byte_size      bigint,
  content_sha256 text,
  -- Where the upload is staged while the import streams it (path or object key; importer-defined).
  file_path      text,
  trace_context  jsonb DEFAULT '{}'::jsonb,
  created_at     timestamptz NOT NULL DEFAULT now(),
  started_at     timestamptz,
  finished_at    timestamptz,
  failure_reason text
);

-- Workers pick up queued/running imports; finished ones are the vast majority and are excluded.
CREATE INDEX feed_imports_active_idx ON feed_imports (created_at) WHERE status IN ('queued', 'running');

CREATE TABLE feed_row_errors (
  id         bigserial PRIMARY KEY,
  feed_id    uuid NOT NULL REFERENCES feed_imports (id) ON DELETE CASCADE,
  -- 1-based data row number (CSV header excluded; JSONL line number).
  row_number integer NOT NULL CHECK (row_number > 0),
  code       text NOT NULL CHECK (code IN ('missing_field', 'invalid_price', 'invalid_currency', 'invalid_json',
                                           'invalid_category', 'too_long', 'duplicate_sku_in_feed',
                                           'malformed_row')),
  field      text,
  message    text NOT NULL,
  -- Raw source line, kept as text because malformed rows are by definition not parseable.
  raw        text,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- GET /feeds/{id}/errors pages by an (feed_id, id) keyset; ids are assigned in row order by the importer.
CREATE INDEX feed_row_errors_feed_idx ON feed_row_errors (feed_id, id);

-- Down Migration

DROP TABLE IF EXISTS feed_row_errors;
DROP TABLE IF EXISTS feed_imports;

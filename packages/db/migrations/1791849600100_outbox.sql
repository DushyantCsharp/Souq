-- Up Migration

-- Transactional outbox: writers insert here in the same transaction as the products/product_texts change;
-- services/outbox-relay publishes to Redpanda `catalog.changes` (key = aggregate_id = sku).
CREATE TABLE outbox (
  id            bigserial PRIMARY KEY,
  aggregate_id  text NOT NULL,
  event_type    text NOT NULL CHECK (event_type IN ('product.upserted', 'product.deleted')),
  -- product_document(sku) at write time (fat event). NOT NULL so an outbox row written before its product
  -- row (product_document() would return NULL) fails loudly instead of publishing an empty event.
  payload       jsonb NOT NULL,
  -- W3C trace carrier ({"traceparent": "...", "tracestate": "..."}) linking feeds -> relay -> indexer.
  -- Nullable: a write outside any trace (backfills, psql) simply has no parent to link.
  trace_context jsonb DEFAULT '{}'::jsonb,
  created_at    timestamptz NOT NULL DEFAULT now(),
  published_at  timestamptz
);

-- The relay polls `WHERE published_at IS NULL ORDER BY id LIMIT n FOR UPDATE SKIP LOCKED`. A partial index
-- keeps that poll proportional to the backlog instead of the table's history.
CREATE INDEX outbox_unpublished_idx ON outbox (id) WHERE published_at IS NULL;
-- Retention job: DELETE ... WHERE published_at < now() - interval '7 days'.
CREATE INDEX outbox_published_at_idx ON outbox (published_at) WHERE published_at IS NOT NULL;
-- "What happened to SKU X?" debugging.
CREATE INDEX outbox_aggregate_idx ON outbox (aggregate_id, id);

-- High insert/update/delete churn: vacuum early so dead tuples do not bloat the partial index the relay scans.
ALTER TABLE outbox SET (autovacuum_vacuum_scale_factor = 0.01, autovacuum_analyze_scale_factor = 0.02);

-- Down Migration

DROP TABLE IF EXISTS outbox;

# ADR 0001: Catalog changes go through a transactional outbox, a Python relay and Redpanda

- Status: Accepted
- Date: 2026-10-14
- Scope: `services/feeds`, `services/outbox-relay`, `services/indexer`, `packages/db` (outbox table),
  Redpanda topics. Wire contracts (topics, headers, payload) are in [CLAUDE.md](../../CLAUDE.md).

## Context

Postgres is the source of truth for the catalog. Search reads OpenSearch. Supplier feeds (CSV/JSONL, up to
500,000 rows in the synthetic test feed) change products in Postgres, and each change has to reach every
OpenSearch index behind the `products_write` alias. The requirements:

- **No lost or invented changes.** A product committed in Postgres must end up in the index. A rolled-back
  import must not.
- **Per-SKU order.** The index must end up holding the newest version of each SKU, even when deliveries are
  duplicated or arrive out of order.
- **Replay.** Rebuilding or catching up an index must not depend on the feed being uploaded again.
- **Lag you can see.** "How far behind is search?" has to be answerable from a metric, per partition.
- **Poison documents stay contained.** One document OpenSearch rejects must not block the documents behind
  it, and it must not be dropped silently either.
- **Freshness.** Target p95 ≤ 30 s from outbox commit to searchable (`index_freshness_seconds`).
- **Small footprint.** The whole stack runs in an ~8 GB Docker VM shared with other work.

Writing to Postgres and to a broker as two independent writes can lose an event (DB commits, publish fails)
or invent one (publish succeeds, DB rolls back).

## Decision

1. **Transactional outbox.** `services/feeds` upserts `products` + `product_texts` and inserts one `outbox`
   row per effective change **in the same transaction**. The row carries `payload = product_document(sku)`
   (the full document, not a diff) and the W3C trace context. `products.version` increments only when
   something changed, so re-importing an unchanged feed produces no events.
2. **Python relay, N replicas.** Each `services/outbox-relay` replica loops: select up to
   `relay.batch_size` (500) unpublished rows `FOR UPDATE SKIP LOCKED`, produce them to `catalog.changes`
   with key = sku, wait for every ack (`acks=all`), set `published_at`, commit. If a replica crashes before
   the commit, the rows are produced again, so delivery is at-least-once. `SKIP LOCKED` gives each replica a
   disjoint set of rows, so failover and extra throughput need no leader election. Published rows are kept
   72 h for debugging, then pruned in chunks (`souq_outbox_relay.config`). The reasons the relay is Python
   (one librdkafka client and one trace-propagation implementation shared with the indexer) are in
   [docs/architecture.md](../architecture.md#why-the-outbox-relay-is-python).
3. **Redpanda (Kafka API).** Topics `catalog.changes` (12 partitions), `catalog.retry.30s` (12),
   `catalog.retry.5m` (12) and `catalog.dlq` (3). Key = sku on every topic, so all events for one SKU land
   on one partition. The producer is idempotent (`enable.idempotence=true`, `acks=all`,
   `max.in.flight.requests.per.connection=5`), so broker-level retries do not reorder or duplicate within a
   partition. The settings live in one place, `souq_common.kafka.producer_config()`.
4. **External versioning in OpenSearch.** The indexer writes with `version_type=external`,
   `version = products.version`, and treats a 409 version conflict as success. That absorbs at-least-once
   duplicates. It also absorbs reordering *across relay replicas*. Two replicas can lock different rows
   for the same SKU and publish v4 before v3. Key = sku keeps each producer's sends in order, not the
   interleaving of two producers. v3 then gets a 409 and the index still converges to v4. Deletes are
   covered the same way while OpenSearch keeps the tombstone (`index.gc_deletes: 1h` in the v1/v2
   templates).
5. **Retry topics and DLQ.** Retryable item failures go to `catalog.retry.30s` and then
   `catalog.retry.5m`, with `attempt` and `not_before` headers (`indexer.retry_delays_s: [30, 300]` after
   `indexer.in_process_attempts: 3`). Non-retryable failures and exhausted retries go to `catalog.dlq` with
   `error_class` / `error_message`. `souq-dlq list | inspect | redrive` handles them. An OpenSearch outage
   is treated as backpressure (AIMD batch sizing, backoff), not as a message failure, so it does not fill
   the retry topics.
6. **Lag is a first-class metric.** The indexer exports `consumer_lag` per partition, `dlq_depth` and
   `index_freshness_seconds`. The Prometheus rules in `infra/prometheus` alert on all three.

## Alternatives considered

| Option | What it would give | Why not |
|---|---|---|
| **Postgres `LISTEN/NOTIFY`** | No extra infrastructure | Notifications are not durable: a listener that is down misses them, so a polling outbox would still be needed for correctness. No replay, no consumer groups, no offset lag to measure, no retry or dead-letter routing. Payload size is limited, so it can't carry a full product document. |
| **SQS / SNS** | Managed queues; FIFO queues with `MessageGroupId = sku` give per-SKU order; redrive policy gives a DLQ; `ApproximateAgeOfOldestMessage` is a usable lag signal | Messages are deleted on ack, so there is no replay from the log, and index rebuilds would always need a full Postgres backfill. Locally it needs an emulator. It also ties a reference implementation that runs on a laptop to one cloud. |
| **RabbitMQ** | Mature routing, TTL + dead-letter exchanges make retry tiers easy | Per-key order needs a consistent-hash exchange plus one consumer per queue. Replay needs RabbitMQ Streams, which is a different API from the queues. Lag is queue depth, not per-partition offset lag. |
| **CDC (Debezium / Kafka Connect)** | No polling; reads the WAL; Debezium's outbox event router could replace the relay | Adds a JVM Connect worker to an 8 GB dev VM. The event router needs custom mapping to turn `outbox.trace_context` into `traceparent`/`tracestate` headers. A replication slot that stops being consumed retains WAL on the primary, which is one more failure mode to operate. |
| **Redpanda Connect** | Lightweight outbox → topic pipe | A second configuration and tracing story next to the Python services. |
| **Apache Kafka instead of Redpanda** | Same API | Nothing is lost by choosing Redpanda: clients use librdkafka, so moving to Kafka or a managed Kafka is a configuration change. Redpanda is a single binary and runs locally with `--memory=768M --smp=1`. |

## Consequences

- **Good.** A product change and its event commit or roll back together, and the relay only publishes
  committed rows. Any single event can rebuild its SKU's document, so the indexer never reads Postgres. The
  log can be replayed and consumer lag can be measured. A poison document waits in the DLQ instead of
  blocking its partition. One trace covers a row from feed upload to bulk write.
- **Duplicates are normal.** Delivery is at-least-once at two points: relay crash before commit, and
  consumer restart before offset commit. Correctness depends on external versioning. Any new consumer that
  writes somewhere other than OpenSearch needs its own idempotency rule.
- **The deletes window is finite.** A stale upsert that arrives more than `gc_deletes` (1 h) after the
  delete of its SKU could bring the product back. In this design that needs a message delayed by over an
  hour, which only a DLQ redrive of an old event could produce. Redrive operators should know this.
- **Write amplification in Postgres.** Every effective change also writes an outbox row with the full
  document. In the 20k-row import profile the outbox insert (`product_document(sku)`, ~0.2 ms per call, not
  inlinable) took 19 % of import wall time. The larger cost, 56 %, was the per-row `product_texts`
  version trigger (see Evidence).
- **Partition count is a ceiling.** 12 partitions cap one indexer consumer group at 12 active consumers.
  Raising it remaps keys to partitions. That is safe for correctness because of external versioning, but
  it is still a planned operation.
- **Fat events.** Every message carries a full document (titles and descriptions in two languages), so
  messages are larger than diffs would be. Accepted, to keep the indexer stateless.
- **Kafka knowledge sits in the Python services.** The TypeScript gateway never produces to Kafka.

## Evidence

| Claim | Measurement | Source |
|---|---|---|
| End-to-end path works and is fresh | 1k-row sample feed: 990 rows accepted, 10 rejected (matches the manifest), 990 docs searchable in `products_read` at their Postgres versions and dual-written to `products_v2`; freshness p50 6.8 s, p95 8.3 s (target ≤ 30 s) | `docs/handoffs/workers-STATUS.md` |
| Import is correct at volume | 500k-row feed: 480 rows/s (1,042 s), exactly 5,000 rejects, matching `supplier_500k.manifest.json` per reject code and row number; unchanged re-import 2,547 rows/s with 0 version bumps and 0 outbox rows. Measured on a scratch DB on the shared Postgres, host load average ~20, so absolute rates are pessimistic | `docs/handoffs/data.md`, `docs/handoffs/workers-STATUS.md` |
| Where import time goes | 20k-row profile: `INSERT INTO product_texts … ON CONFLICT` 56 % of wall time (per-row `product_texts_touch_product` trigger → two extra non-HOT product updates per new SKU), outbox insert 19 %, products upsert 9 %. Proposed fix: a statement-level trigger | `docs/handoffs/data.md` |
| No loss or duplication under an outage | Chaos test: OpenSearch killed mid-import, then restarted. Passes with zero lost and zero duplicated SKUs (OpenSearch count = Postgres count, every `_version` = `products.version`) | `tests/chaos/test_opensearch_outage.py` |
| Two relays never double-publish | `test_two_replicas_never_publish_a_row_twice` passes; `test_publishes_marks_and_preserves_per_sku_order` passes | `services/outbox-relay/tests/test_relay_integration.py` |
| Reordering and redelivery are harmless | Stale version does not overwrite a newer one; redelivered same version is a no-op; a stale upsert cannot bring back a deleted product | `tests/integration/test_external_versioning.py` |
| Relay throughput ceiling, consumer lag during the 500k import, broker resource use at scale | not measured | — |

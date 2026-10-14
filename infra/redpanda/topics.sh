#!/usr/bin/env bash
# Idempotent topic bootstrap. Key = sku everywhere, so partition count fixes per-SKU ordering scope.
set -euo pipefail
B=redpanda:9092
create() { rpk topic create "$1" -p "$2" -r 1 -c "$3" -X brokers=$B 2>&1 | grep -v "TOPIC_ALREADY_EXISTS" || true; }
create catalog.changes   12 retention.ms=604800000
create catalog.retry.30s 12 retention.ms=604800000
create catalog.retry.5m  12 retention.ms=604800000
create catalog.dlq        3 retention.ms=2592000000
rpk topic list -X brokers=$B

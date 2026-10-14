#!/usr/bin/env bash
# Run loadtest/search.js with the grafana/k6 image (k6 is not installed on the dev machine).
#
#   loadtest/run.sh                                   # 200 RPS, 5 min, against the gateway on the host (:4000)
#   RATE=50 DURATION=1m loadtest/run.sh               # smoke
#   K6_NETWORK=souq_default BASE_URL=http://gateway:4000 loadtest/run.sh   # skip host NAT: join the compose net
#   K6_PROMETHEUS_RW=1 loadtest/run.sh                # also stream k6 metrics into Prometheus (obs profile)
#
# Holds the shared heavy-docker lock for the whole run (Docker VM memory is shared with other work).
# Results: loadtest/results/summary.{md,json} (+ a timestamped copy). Extra args are passed to `k6 run`.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE="${K6_IMAGE:-grafana/k6:2.3.0}"
LOCK="$ROOT/.locks/heavy-docker"

mkdir -p "$ROOT/.locks" "$ROOT/loadtest/results"
until mkdir "$LOCK" 2>/dev/null; do
  echo "waiting for heavy-docker lock ($LOCK)..." >&2
  sleep 15
done
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT INT TERM

net_args=(--add-host=host.docker.internal:host-gateway)
default_base="http://host.docker.internal:4000"
if [[ -n "${K6_NETWORK:-}" ]]; then
  net_args=(--network "$K6_NETWORK")
  default_base="http://gateway:4000"
fi

out_args=()
env_args=()
if [[ -n "${K6_PROMETHEUS_RW:-}" ]]; then
  prom="http://host.docker.internal:9091/api/v1/write"
  [[ -n "${K6_NETWORK:-}" ]] && prom="http://prometheus:9090/api/v1/write"
  out_args=(-o experimental-prometheus-rw)
  env_args+=(-e "K6_PROMETHEUS_RW_SERVER_URL=$prom" -e "K6_PROMETHEUS_RW_TREND_STATS=p(50),p(95),p(99),max")
fi

for var in RATE DURATION CACHE_MODE DEEP_PAGES API_KEY CLIENT_IPS SLO_STANDARD_MS SLO_LLM_MS; do
  [[ -n "${!var:-}" ]] && env_args+=(-e "$var=${!var}")
done

docker run --rm --name "souq-k6-$$" "${net_args[@]}" \
  --memory 768m \
  -v "$ROOT/loadtest:/loadtest" -w /loadtest \
  -e "BASE_URL=${BASE_URL:-$default_base}" \
  ${env_args[@]+"${env_args[@]}"} \
  "$IMAGE" run ${out_args[@]+"${out_args[@]}"} "$@" search.js

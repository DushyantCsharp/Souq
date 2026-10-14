#!/usr/bin/env bash
# Prove the node-pg-migrate migrations are reversible, against a scratch database:
#
#   d0 = schema before  ->  up  ->  d1  ->  down all  ->  d2 (== d0)  ->  up  ->  d3 (== d1)  ->  up (no-op)
#
# A down section that forgets an object shows up as d2 != d0; one that drops something `up` does not
# recreate identically shows up as d3 != d1.
#
#   DATABASE_URL=postgres://souq:souq@localhost:5432/souq \
#   PG_DUMP="docker exec <postgres-container> pg_dump -U souq -d souq" \
#   ci/migrations_roundtrip.sh [--include-next]
#
# Extra args go to every migrate call (--include-next also round-trips the staged contract migrations).
# NEXT_CYCLE=1 then applies the staged contract migrations (packages/db/migrations-next) on top and rolls
# back exactly those: the schema must return to the current release's, i.e. the contract step's down
# restores what the running release still reads.
# PG_DUMP defaults to the local pg_dump against DATABASE_URL; it must be >= the server's major version,
# which is why CI runs the server container's own pg_dump.
set -euo pipefail

: "${DATABASE_URL:?DATABASE_URL must point at a scratch database (this script runs down all)}"
PG_DUMP=${PG_DUMP:-"pg_dump $DATABASE_URL"}
MIGRATE=${MIGRATE:-"pnpm --silent --filter @souq/db run migrate"}
NEXT_DIR=${NEXT_MIGRATIONS_DIR:-packages/db/migrations-next}
TABLE=${MIGRATIONS_TABLE:-pgmigrations}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

dump() {
  # \restrict/\unrestrict lines carry a random per-dump key (pg_dump >= 16.10). Comment-only lines are
  # dropped too, including inside function bodies: a down section that recreates a function without its
  # comments is behaviourally identical and should not fail the round-trip.
  # shellcheck disable=SC2086 # PG_DUMP is a command line on purpose
  $PG_DUMP --schema-only --no-owner --no-privileges --exclude-table="public.${TABLE}*" \
    | grep -Ev '^[[:space:]]*--|^\\(un)?restrict ' | sed '/^$/d' >"$1"
}

migrate() {
  echo "::group::migrate $*"
  # shellcheck disable=SC2086 # MIGRATE is a command line on purpose
  $MIGRATE "$@"
  echo "::endgroup::"
}

same() { # same <label> <a> <b> [grep -v pattern]
  local label=$1 a=$2 b=$3 ignore=${4:-}
  if [[ -n $ignore ]]; then
    grep -Ev "$ignore" "$a" >"$a.cmp" || true
    grep -Ev "$ignore" "$b" >"$b.cmp" || true
    a=$a.cmp b=$b.cmp
  fi
  if ! diff -u "$a" "$b" >"$work/diff"; then
    echo "::error title=migration round-trip::$label"
    cat "$work/diff"
    return 1
  fi
  echo "ok: $label"
}

dump "$work/d0"
migrate up "$@"
dump "$work/d1"
if [[ $(wc -l <"$work/d1") -le $(wc -l <"$work/d0") ]]; then
  echo "::error title=migration round-trip::'up' did not change the schema (wrong DATABASE_URL?)"
  exit 1
fi
migrate down all "$@"
dump "$work/d2"
# Extensions may legitimately pre-exist (infra/postgres/init) and survive a full rollback.
same "down all restores the pre-migration schema" "$work/d0" "$work/d2" '^(CREATE|COMMENT ON) EXTENSION'
migrate up "$@"
dump "$work/d3"
same "re-applying after rollback reproduces the schema" "$work/d1" "$work/d3"
migrate up "$@"
dump "$work/d4"
same "a second up is a no-op" "$work/d3" "$work/d4"

if [[ ${NEXT_CYCLE:-0} == 1 ]]; then
  shopt -s nullglob
  staged=("$NEXT_DIR"/*.sql)
  if ((${#staged[@]} == 0)); then
    echo "ok: no staged contract migrations in $NEXT_DIR"
  else
    migrate up --include-next
    dump "$work/d5"
    if cmp -s "$work/d4" "$work/d5"; then
      echo "::error title=migration round-trip::applying $NEXT_DIR changed nothing"
      exit 1
    fi
    migrate down "${#staged[@]}" --include-next
    dump "$work/d6"
    same "rolling back ${#staged[@]} contract migration(s) restores the current schema" "$work/d4" "$work/d6"
  fi
fi
echo "migrations round-trip OK ($(grep -c '^CREATE' "$work/d1") CREATE statements)"

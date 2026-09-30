#!/usr/bin/env bash
# Truncate the four DELPHI data tables before a Phase-A bootstrap so the
# resulting seed is generated against a clean history surface (no
# smoke-test residue, no calibration probes, no prior bootstrap rows).
#
# Idempotent: the script reports row counts before and after, then exits
# non-zero if any of the four tables still has rows. Running it a second
# time on an already-clean DB is a no-op.
#
# Operator action. Requires:
#   1. experiments/db/.env populated (see .env.example)
#   2. an open port-forward to svc/postgres on on-prem (port-forward.ps1)
#   3. psql on PATH
#
# Tables truncated (TRUNCATE ... RESTART IDENTITY CASCADE):
#   decision_requests  - request lifecycle state
#   outbox             - transactional outbox rows (FK -> decision_requests)
#   cluster_snapshots  - cached cluster status snapshots
#   job_metrics        - per-job sidecar telemetry
#
# Tables explicitly NOT truncated:
#   schema_migrations  - decision-maker migration tracking; truncating
#                        would re-trigger every migration on the next
#                        decision-maker startup, which is fine but
#                        wastes time and is outside the data-plane
#                        cleanup we want here.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"

if [[ ! -f "${ENV_FILE}" ]]; then
    echo "error: ${ENV_FILE} not found. Copy .env.example -> .env and fill in DB_PASSWORD." >&2
    exit 1
fi

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

: "${DB_HOST:?DB_HOST not set in .env}"
: "${DB_PORT:?DB_PORT not set in .env}"
: "${DB_USER:?DB_USER not set in .env}"
: "${DB_NAME:?DB_NAME not set in .env}"
: "${DB_PASSWORD:?DB_PASSWORD not set in .env}"

TABLES=(decision_requests outbox cluster_snapshots job_metrics)

psql_cmd() {
    PGPASSWORD="${DB_PASSWORD}" psql \
        --host "${DB_HOST}" --port "${DB_PORT}" \
        --username "${DB_USER}" --dbname "${DB_NAME}" \
        --no-psqlrc --quiet --tuples-only --no-align \
        "$@"
}

count_rows() {
    local label="$1"
    echo "==> ${label} row counts:"
    for t in "${TABLES[@]}"; do
        # \pset border 0 is the default; tuples-only + no-align gives a bare number.
        local n
        n="$(psql_cmd -c "SELECT count(*) FROM ${t};" | tr -d '[:space:]')"
        printf "    %-20s %s\n" "${t}" "${n}"
    done
}

if [[ "${1:-}" == "--count-only" ]]; then
    count_rows "current"
    exit 0
fi

count_rows "before"

echo "==> TRUNCATE TABLE ${TABLES[*]} RESTART IDENTITY CASCADE"
psql_cmd -c "TRUNCATE TABLE $(IFS=, ; echo "${TABLES[*]}") RESTART IDENTITY CASCADE;"

count_rows "after"

# Verify every target table reaches 0. Treat any non-zero count as failure
# so a partial truncate cannot be mistaken for success.
fail=0
for t in "${TABLES[@]}"; do
    n="$(psql_cmd -c "SELECT count(*) FROM ${t};" | tr -d '[:space:]')"
    if [[ "${n}" != "0" ]]; then
        echo "error: table ${t} still has ${n} row(s) after TRUNCATE" >&2
        fail=1
    fi
done

if [[ "${fail}" -ne 0 ]]; then
    exit 1
fi

echo "==> done: 4 tables empty"

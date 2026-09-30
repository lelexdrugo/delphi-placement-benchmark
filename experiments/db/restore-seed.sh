#!/usr/bin/env bash
# Restore a Phase-A seed dump into the decision-maker Postgres.
#
# Operator action. WARNING: this drops and recreates all data in the target
# database. Requires explicit -y acknowledgement and a seed file path.
#
# Usage:
#   bash experiments/db/restore-seed.sh -y experiments/runs/phase-a/seed-<ts>.sql
#
# Validates the .sha256 sidecar when present.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"

if [[ "${1:-}" != "-y" ]]; then
    cat >&2 <<EOF
refusing to overwrite the database without explicit acknowledgement.

usage:
    bash $0 -y <seed-file>

This script is destructive: every row in ${DB_NAME:-jobmetricsdb} is replaced.
Make sure the running decision-maker / placer-management can tolerate a
restart, and that you have a current dump of the live state if you need to
roll back. Re-run with -y to proceed.
EOF
    exit 1
fi
seed="${2:-}"
if [[ -z "${seed}" || ! -f "${seed}" ]]; then
    echo "error: seed file '${seed}' not found." >&2
    exit 1
fi

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

# Validate the SHA-256 sidecar if present.
sidecar="${seed}.sha256"
if [[ -f "${sidecar}" ]]; then
    # Take the hash field only. dump-seed.sh writes a hash-only sidecar, but a
    # sidecar produced by `sha256sum`/`sha256sum -b` carries a trailing
    # ` <file>` / ` *<file>`; awk '{print $1}' tolerates both forms.
    expected="$(awk '{print $1}' "${sidecar}" | tr -d '[:space:]')"
    if command -v sha256sum >/dev/null 2>&1; then
        actual="$(sha256sum "${seed}" | awk '{print $1}')"
    elif command -v shasum >/dev/null 2>&1; then
        actual="$(shasum -a 256 "${seed}" | awk '{print $1}')"
    else
        actual=""
    fi
    if [[ -n "${actual}" && "${expected}" != "${actual}" ]]; then
        echo "error: sha256 mismatch" >&2
        echo "  expected: ${expected}" >&2
        echo "  actual:   ${actual}" >&2
        exit 1
    fi
    if [[ -n "${actual}" ]]; then
        echo "==> sha256 verified (${actual})"
    fi
fi

echo "==> restoring ${seed} -> ${DB_NAME} @ ${DB_HOST}:${DB_PORT}"
PGPASSWORD="${DB_PASSWORD}" psql \
    --host "${DB_HOST}" --port "${DB_PORT}" \
    --username "${DB_USER}" --dbname "${DB_NAME}" \
    --single-transaction --set ON_ERROR_STOP=on \
    --file "${seed}"
echo "==> done: ${seed}"

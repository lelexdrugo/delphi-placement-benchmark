#!/usr/bin/env bash
# Dump the decision-maker Postgres into a SHA-tagged seed file.
#
# Operator action. Requires:
#   1. experiments/db/.env populated with the real password (see .env.example)
#   2. an open port-forward to svc/postgres (see port-forward.ps1)
#   3. pg_dump on PATH (any modern Postgres client works against the server version)
#
# Usage:
#   bash experiments/db/dump-seed.sh [output-path]
#
# When output-path is supplied, the SQL dump is written there (parent
# directory is created if needed). Otherwise the dump goes to
# experiments/runs/phase-a/seed-<UTC timestamp>.sql with the matching
# .sha256 sidecar. iter-4c uses the explicit-path form for the
# defensive pre-bootstrap snapshot
# (`experiments/runs/phase-a/pre-bootstrap-<ts>.sql`) so the file is
# unambiguous later.
#
# Output (default):
#   experiments/runs/phase-a/seed-<UTC timestamp>.sql
#   experiments/runs/phase-a/seed-<UTC timestamp>.sql.sha256

set -euo pipefail

# Locate this script and resolve repo-relative paths.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"
OUT_DIR="${SCRIPT_DIR}/../runs/phase-a"

if [[ ! -f "${ENV_FILE}" ]]; then
    echo "error: ${ENV_FILE} not found. Copy .env.example -> .env and fill in DB_PASSWORD." >&2
    exit 1
fi

# Load env (set -a exports every variable from .env automatically).
set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

: "${DB_HOST:?DB_HOST not set in .env}"
: "${DB_PORT:?DB_PORT not set in .env}"
: "${DB_USER:?DB_USER not set in .env}"
: "${DB_NAME:?DB_NAME not set in .env}"
: "${DB_PASSWORD:?DB_PASSWORD not set in .env}"

ts="$(date -u +%Y%m%dT%H%M%SZ)"
if [[ -n "${1:-}" ]]; then
    out="$1"
else
    out="${OUT_DIR}/seed-${ts}.sql"
fi
mkdir -p "$(dirname "${out}")"

echo "==> dumping ${DB_NAME} @ ${DB_HOST}:${DB_PORT} -> ${out}"
# --clean --if-exists emits "DROP TABLE IF EXISTS ... CASCADE" before each
# CREATE TABLE so restore-seed.sh can run against a non-empty target DB.
# Without these flags, restoring into an already-populated database fails
# at the first duplicate-key insert.
PGPASSWORD="${DB_PASSWORD}" pg_dump \
    --host "${DB_HOST}" --port "${DB_PORT}" \
    --username "${DB_USER}" --dbname "${DB_NAME}" \
    --clean --if-exists \
    --no-owner --no-privileges \
    --file "${out}"

# Portable SHA computation: prefer sha256sum (Linux/WSL), fall back to shasum -a 256 (macOS).
if command -v sha256sum >/dev/null 2>&1; then
    sha="$(sha256sum "${out}" | awk '{print $1}')"
elif command -v shasum >/dev/null 2>&1; then
    sha="$(shasum -a 256 "${out}" | awk '{print $1}')"
else
    echo "warning: no sha256sum or shasum on PATH; skipping .sha256 sidecar" >&2
    sha=""
fi

if [[ -n "${sha}" ]]; then
    echo "${sha}" > "${out}.sha256"
    echo "    sha256: ${sha}"
fi
echo "==> done: ${out}"

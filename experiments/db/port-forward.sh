#!/usr/bin/env bash
# Idempotent kubectl port-forward to the decision-maker Postgres.
# Bash sibling of port-forward.ps1 (iter-4a). Same .env contract:
# PG_K8S_CONTEXT / PG_K8S_NAMESPACE / PG_K8S_SERVICE / PG_K8S_PORT /
# PG_LOCAL_PORT, all optional with safe defaults.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
PID_FILE="$SCRIPT_DIR/.port-forward.pid"
OUT_FILE="$SCRIPT_DIR/.port-forward.out"
ERR_FILE="$SCRIPT_DIR/.port-forward.err"

STOP=0
FOREGROUND=0
LOCAL_PORT_OVERRIDE=0
WAIT_SECONDS=10

while [[ $# -gt 0 ]]; do
    case "$1" in
        --stop) STOP=1; shift ;;
        --foreground) FOREGROUND=1; shift ;;
        --local-port) LOCAL_PORT_OVERRIDE="$2"; shift 2 ;;
        --wait-seconds) WAIT_SECONDS="$2"; shift 2 ;;
        -h|--help)
            grep '^#' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done

# Load .env if present.
if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

CTX="${PG_K8S_CONTEXT:-on-prem}"
NS="${PG_K8S_NAMESPACE:-karmada-system}"
SVC="${PG_K8S_SERVICE:-postgres}"
REMOTE="${PG_K8S_PORT:-5432}"
if [[ "$LOCAL_PORT_OVERRIDE" -gt 0 ]]; then
    EFFECTIVE="$LOCAL_PORT_OVERRIDE"
else
    EFFECTIVE="${PG_LOCAL_PORT:-5432}"
fi

read_pid() { [[ -f "$PID_FILE" ]] && cat "$PID_FILE" || true; }
pid_alive() {
    local id="$1"
    [[ -n "$id" ]] && kill -0 "$id" 2>/dev/null
}
port_open() {
    if command -v nc >/dev/null 2>&1; then
        nc -z -w 1 127.0.0.1 "$1" 2>/dev/null
    elif command -v ss >/dev/null 2>&1; then
        ss -ltn "( sport = :$1 )" | grep -q ":$1"
    else
        # Fallback: bash /dev/tcp
        (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && exec 3>&-
    fi
}

if [[ "$STOP" -eq 1 ]]; then
    held="$(read_pid)"
    if [[ -z "$held" ]]; then
        echo "==> no held port-forward (no PID file)"
        rm -f "$PID_FILE"
        exit 0
    fi
    if pid_alive "$held"; then
        echo "==> stopping kubectl port-forward (PID $held)"
        kill "$held" 2>/dev/null || true
        sleep 0.5
        kill -9 "$held" 2>/dev/null || true
    else
        echo "==> held PID $held is not running; cleaning up PID file"
    fi
    rm -f "$PID_FILE"
    exit 0
fi

if [[ "$FOREGROUND" -eq 1 ]]; then
    echo "==> foreground port-forward svc/$SVC -n $NS on $CTX (${EFFECTIVE}:${REMOTE})"
    exec kubectl --context "$CTX" port-forward -n "$NS" "svc/$SVC" "${EFFECTIVE}:${REMOTE}"
fi

held="$(read_pid)"
if [[ -n "$held" ]] && pid_alive "$held" && port_open "$EFFECTIVE"; then
    echo "==> port-forward already running (PID $held, localhost:$EFFECTIVE)"
    exit 0
fi

if [[ -n "$held" ]] && pid_alive "$held"; then
    echo "==> stale state: PID $held alive but port $EFFECTIVE not answering; restarting"
    kill "$held" 2>/dev/null || true
fi
rm -f "$PID_FILE"

echo "==> starting port-forward svc/$SVC -n $NS on $CTX (localhost:$EFFECTIVE -> :$REMOTE)"
nohup kubectl --context "$CTX" port-forward -n "$NS" "svc/$SVC" "${EFFECTIVE}:${REMOTE}" \
    >"$OUT_FILE" 2>"$ERR_FILE" &
new_pid=$!
echo "$new_pid" > "$PID_FILE"
echo "    spawned PID $new_pid; waiting up to ${WAIT_SECONDS}s for localhost:$EFFECTIVE ..."

deadline=$(( $(date +%s) + WAIT_SECONDS ))
while [[ "$(date +%s)" -lt "$deadline" ]]; do
    if ! pid_alive "$new_pid"; then
        echo "==> kubectl exited prematurely; see $ERR_FILE" >&2
        rm -f "$PID_FILE"
        exit 1
    fi
    if port_open "$EFFECTIVE"; then
        echo "==> ready"
        exit 0
    fi
    sleep 0.25
done

echo "==> timed out after ${WAIT_SECONDS}s waiting for localhost:$EFFECTIVE" >&2
kill "$new_pid" 2>/dev/null || true
rm -f "$PID_FILE"
exit 1

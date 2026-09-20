#!/usr/bin/env bash
set -u

APP="/opt/SBR-Pager-Gateway"
ENV_FILE="$APP/.env"
CONTAINER="racher-sms-gateway"
HEALTH_URL="http://127.0.0.1:8090/health"
LOCK_FILE="/run/lock/sbr-modem-watchdog.lock"
STATE_DIR="/var/lib/sbr-modem-watchdog"
LAST_RECOVERY_FILE="$STATE_DIR/last-recovery"

log() {
    logger -t sbr-modem-watchdog "$*"
    echo "$*"
}

env_value() {
    local key="$1"
    if [[ ! -f "$ENV_FILE" ]]; then
        return 0
    fi
    grep -E "^${key}=" "$ENV_FILE" 2>/dev/null \
      | tail -1 \
      | cut -d= -f2-
}

check_modem() {
    python3 - <<'PY'
import json
import sys
import urllib.request

try:
    with urllib.request.urlopen(
        "http://127.0.0.1:8090/health",
        timeout=6,
    ) as response:
        data = json.load(response)
except Exception as exc:
    print(f"health-fejl={exc}")
    sys.exit(2)

gateway = data.get("gateway") or {}
modem = data.get("modem") or {}

gateway_state = str(gateway.get("state", "unknown")).lower()
modem_state = str(modem.get("state", "unknown")).lower()

print(
    f"gateway={gateway_state} "
    f"modem={modem_state} "
    f"error={modem.get('last_error')}"
)

if gateway_state == "online" and modem_state == "online":
    sys.exit(0)

sys.exit(1)
PY
}

host_modem_device() {
    local mapped configured

    mapped="$(
        docker inspect "$CONTAINER" \
          --format '{{range .HostConfig.Devices}}{{if eq .PathInContainer "/dev/sbr-sms-modem"}}{{.PathOnHost}}{{end}}{{end}}' \
          2>/dev/null || true
    )"

    if [[ -n "$mapped" ]]; then
        printf '%s\n' "$mapped"
        return 0
    fi

    configured="$(env_value SMS_MODEM_HOST_DEVICE)"
    if [[ -n "$configured" ]]; then
        printf '%s\n' "$configured"
    fi
}

mkdir -p "$STATE_DIR"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    exit 0
fi

if check_modem >/dev/null 2>&1; then
    exit 0
fi

log "⚠️ Modem-health fejler. Dobbelttjekker om 10 sekunder."
sleep 10

if check_modem >/dev/null 2>&1; then
    log "✅ Modemmet kom selv tilbage."
    exit 0
fi

HOST_DEVICE="$(host_modem_device || true)"

if [[ -z "$HOST_DEVICE" ]]; then
    log "⚠️ Kunne ikke fastslå host-enheden for /dev/sbr-sms-modem. Genstarter ikke Gateway."
    exit 0
fi

if [[ ! -e "$HOST_DEVICE" ]]; then
    log "⚠️ SMS-modem-enheden mangler på hosten: $HOST_DEVICE. Genstarter ikke Gateway."
    exit 0
fi

COOLDOWN="$(env_value SBR_MODEM_WATCHDOG_RECOVERY_COOLDOWN_SECONDS)"
COOLDOWN="${COOLDOWN:-600}"

if ! [[ "$COOLDOWN" =~ ^[0-9]+$ ]]; then
    COOLDOWN=600
fi

NOW="$(date +%s)"
LAST=0

if [[ -f "$LAST_RECOVERY_FILE" ]]; then
    read -r LAST < "$LAST_RECOVERY_FILE" || LAST=0
fi

if ! [[ "$LAST" =~ ^[0-9]+$ ]]; then
    LAST=0
fi

AGE=$((NOW - LAST))

if (( AGE < COOLDOWN )); then
    REMAIN=$((COOLDOWN - AGE))
    log "⚠️ Modemmet er fortsat offline, men recovery er i cooldown (${REMAIN}s tilbage)."
    exit 0
fi

printf '%s\n' "$NOW" > "$LAST_RECOVERY_FILE"

log "⚠️ Modem-enheden findes, men health er stadig nede. Genstarter SMS Gateway én gang."

if ! docker restart "$CONTAINER" >/dev/null; then
    log "❌ Kunne ikke genstarte $CONTAINER"
    exit 1
fi

sleep 20

if check_modem >/dev/null 2>&1; then
    log "✅ Modemmet kom tilbage efter Gateway-genstart."
    exit 0
fi

RESULT="$(check_modem 2>&1 || true)"
log "⚠️ Modemmet er stadig offline efter Gateway-genstart: $RESULT"
log "ℹ️ Automatisk USB-reset er deaktiveret. Der foretages ikke model-specifik hardware-recovery."

exit 0

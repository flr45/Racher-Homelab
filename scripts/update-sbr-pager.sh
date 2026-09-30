#!/usr/bin/env bash
# Run on racherserver after checking out the reviewed update branch.
set -Eeuo pipefail
umask 077

APP_DIR="${SBR_PAGER_ROOT:-/opt/SBR-Pager-Gateway}"
cd "$APP_DIR"
ENV_FILE="$APP_DIR/.env"
[[ -f "$ENV_FILE" ]] || { echo "Mangler $ENV_FILE" >&2; exit 1; }
command -v docker >/dev/null

DRIVER="$(sed -n 's/^SMS_MODEM_DRIVER=//p' "$ENV_FILE" | tail -1 | tr -d '\r\"\047')"
DRIVER="${DRIVER:-usb}"
case "$DRIVER" in
  usb) GW_COMPOSE="$APP_DIR/compose/sms-gateway/docker-compose.yml" ;;
  cudy) GW_COMPOSE="$APP_DIR/compose/sms-gateway/cudy.yml" ;;
  *) echo "SMS_MODEM_DRIVER skal være usb eller cudy." >&2; exit 1 ;;
esac
WA_COMPOSE="$APP_DIR/compose/sms-whatsapp/compose.yml"
WA=(docker compose --env-file "$ENV_FILE" -f "$WA_COMPOSE")
GW=(docker compose --env-file "$ENV_FILE" -f "$GW_COMPOSE")
"${WA[@]}" config --quiet
"${GW[@]}" config --quiet

STAMP="$(date -u +%Y%m%dT%H%M%SZ)-$$"
BACKUP_DIR="$APP_DIR/manual-backups/pager-update-$STAMP"
mkdir -p "$BACKUP_DIR"
cp "$ENV_FILE" "$BACKUP_DIR/env.backup"
git rev-parse HEAD > "$BACKUP_DIR/checkout.txt"

backup_db() {
  local container="$1" database="$2" staged="/tmp/sbr-pager-update-$$.db"
  docker exec "$container" python -c '
import sqlite3, sys
source = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
target = sqlite3.connect(sys.argv[2])
source.backup(target)
assert target.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
target.close(); source.close()
' "/data/$database" "$staged"
  docker cp "$container:$staged" "$BACKUP_DIR/$database"
  docker exec "$container" rm -f "$staged"
  chmod 600 "$BACKUP_DIR/$database"
}
backup_db sbr-sms-whatsapp sms-whatsapp.db
backup_db racher-sms-gateway sms-gateway.db

PAGER_ROLLBACK="sbr-pager-rollback:$STAMP"
GATEWAY_ROLLBACK="sbr-gateway-rollback:$STAMP"
docker tag "$(docker inspect sbr-sms-whatsapp --format '{{.Image}}')" "$PAGER_ROLLBACK"
docker tag "$(docker inspect racher-sms-gateway --format '{{.Image}}')" "$GATEWAY_ROLLBACK"
printf 'services:\n  sms-whatsapp:\n    image: %s\n' "$PAGER_ROLLBACK" > "$BACKUP_DIR/rollback-pager.yml"
printf 'services:\n  sms-gateway:\n    image: %s\n' "$GATEWAY_ROLLBACK" > "$BACKUP_DIR/rollback-gateway.yml"

echo "Backup og tidligere images er klar. Bygger den nye version."
"${WA[@]}" build sms-whatsapp
"${GW[@]}" build sms-gateway

SUDO=()
[[ "$(id -u)" == 0 ]] || SUDO=(sudo)
ACTIVE_TIMERS=()
restore_timers() {
  for timer in "${ACTIVE_TIMERS[@]}"; do
    "${SUDO[@]}" systemctl start "$timer" || true
  done
}
trap restore_timers EXIT
for timer in sbr-pager-watchdog.timer sbr-modem-watchdog.timer; do
  if systemctl is-active --quiet "$timer"; then
    ACTIVE_TIMERS+=("$timer")
    "${SUDO[@]}" systemctl stop "$timer"
  fi
done
# A service started just before its timer stopped must also finish first.
for service in sbr-pager-watchdog.service sbr-modem-watchdog.service; do
  if systemctl is-active --quiet "$service"; then
    "${SUDO[@]}" systemctl stop "$service"
  fi
done
rollback() {
  echo "Opdatering fejlede. Gendanner de tidligere images." >&2
  docker compose --env-file "$ENV_FILE" -f "$WA_COMPOSE" -f "$BACKUP_DIR/rollback-pager.yml" up -d --no-build --no-deps sms-whatsapp || true
  docker compose --env-file "$ENV_FILE" -f "$GW_COMPOSE" -f "$BACKUP_DIR/rollback-gateway.yml" up -d --no-build --no-deps sms-gateway || true
}
trap 'rollback; exit 1' ERR
trap 'rollback; exit 130' INT
trap 'rollback; exit 143' TERM

# OpenWA is deliberately not recreated: keep the existing WhatsApp session.
"${WA[@]}" up -d --no-build --no-deps sms-whatsapp
"${GW[@]}" up -d --no-build --no-deps sms-gateway

check_health() {
  docker exec sbr-sms-whatsapp python -c 'import json,urllib.request; d=json.load(urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=6)); assert d["database"] == "online"' >/dev/null 2>&1 &&
  docker exec racher-sms-gateway python -c 'import json,urllib.request; d=json.load(urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=6)); assert d["gateway"]["database"] == "online" and d["modem"]["state"] == "online"' >/dev/null 2>&1
}
READY=false
for ((attempt = 0; attempt < 18; attempt++)); do
  if check_health; then READY=true; break; fi
  sleep 5
done
if [[ "$READY" != true ]]; then
  echo "Modem og databaser blev ikke klar. Se containerlogs." >&2
  rollback
  exit 1
fi
trap - ERR
trap - INT TERM
echo "Opdateringen er startet. SMS-kilde: $DRIVER."
echo "Backup og rollback-filer: $BACKUP_DIR"
echo "Kontrollér modem, WhatsApp og leveringer på overblikket. Ingen testbesked er sendt."

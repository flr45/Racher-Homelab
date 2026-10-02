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
DRIVER="${SBR_PAGER_TARGET_DRIVER:-$DRIVER}"
case "$DRIVER" in
  usb) GW_COMPOSE="$APP_DIR/compose/sms-gateway/docker-compose.yml" ;;
  cudy) GW_COMPOSE="$APP_DIR/compose/sms-gateway/cudy.yml" ;;
  *) echo "SMS_MODEM_DRIVER skal være usb eller cudy." >&2; exit 1 ;;
esac
WA_COMPOSE="$APP_DIR/compose/sms-whatsapp/compose.yml"
WA=(docker compose --env-file "$ENV_FILE" -f "$WA_COMPOSE")
OFFSITE_MOUNT="$(sed -n 's/^SMS_WHATSAPP_OFFSITE_MOUNT=//p' "$ENV_FILE" | tail -1 | tr -d '\r\"\047')"
if [[ "$OFFSITE_MOUNT" == true ]]; then
  WA+=(-f "$APP_DIR/compose/sms-whatsapp/offsite-backup.yml")
fi
GW=(docker compose --env-file "$ENV_FILE" -f "$GW_COMPOSE")
"${WA[@]}" config --quiet
"${GW[@]}" config --quiet

STAMP="$(date -u +%Y%m%dT%H%M%SZ)-$$"
BACKUP_DIR="$APP_DIR/manual-backups/pager-update-$STAMP"
mkdir -p "$BACKUP_DIR"
cp "$ENV_FILE" "$BACKUP_DIR/env.backup"
# During a USB -> Cudy transition the caller saves the original environment
# before entering router credentials. Use that private copy for rollback.
if [[ -n "${SBR_PAGER_ROLLBACK_ENV:-}" ]]; then
  [[ -f "$SBR_PAGER_ROLLBACK_ENV" ]] || { echo "Rollback-miljøfil mangler." >&2; exit 1; }
  cp "$SBR_PAGER_ROLLBACK_ENV" "$BACKUP_DIR/env.backup"
fi
chmod 600 "$BACKUP_DIR/env.backup"
OLD_DRIVER="$(sed -n 's/^SMS_MODEM_DRIVER=//p' "$BACKUP_DIR/env.backup" | tail -1 | tr -d '\r\"\047')"
case "${OLD_DRIVER:-usb}" in
  usb) OLD_GW_COMPOSE="$APP_DIR/compose/sms-gateway/docker-compose.yml" ;;
  cudy) OLD_GW_COMPOSE="$APP_DIR/compose/sms-gateway/cudy.yml" ;;
  *) echo "Ukendt modemtype i rollback-miljøfil." >&2; exit 1 ;;
esac
git rev-parse HEAD > "$BACKUP_DIR/checkout.txt"

SUDO=()
[[ "$(id -u)" == 0 ]] || SUDO=(sudo)
ACTIVE_TIMERS=()
restore_timers() {
  for timer in "${ACTIVE_TIMERS[@]}"; do
    "${SUDO[@]}" systemctl start "$timer" || true
  done
}
trap restore_timers EXIT
# Before image replacement, failures leave services intact and restore the
# original driver/environment before the watchdog timers are resumed.
trap 'cp "$BACKUP_DIR/env.backup" "$ENV_FILE"; chmod 600 "$ENV_FILE"; exit 1' ERR
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

if [[ -n "${SBR_PAGER_TARGET_DRIVER:-}" ]]; then
  # Change the selected driver only after its watchdogs have stopped.
  sed '/^SMS_MODEM_DRIVER=/d' "$ENV_FILE" > "$BACKUP_DIR/env.target"
  printf 'SMS_MODEM_DRIVER=%s\n' "$DRIVER" >> "$BACKUP_DIR/env.target"
  cp "$BACKUP_DIR/env.target" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
fi

backup_db() {
  local container="$1" database="$2" staged="/tmp/sbr-pager-update-$$.db"
  if [[ "$(docker inspect "$container" --format '{{.State.Running}}')" == false ]]; then
    # A stopped USB container cannot be started without its physical device.
    # Copy its entire data directory (including WAL) and back up that private
    # snapshot with the existing image, without its entrypoint or network.
    local snapshot="$BACKUP_DIR/$container-source" image
    mkdir -m 700 "$snapshot"
    docker cp "$container:/data/." "$snapshot"
    [[ "$(docker inspect "$container" --format '{{.State.Running}}')" == false ]] || {
      echo "Containeren startede under backup; afbryder uden installation." >&2; return 1;
    }
    image="$(docker inspect "$container" --format '{{.Image}}')"
    docker run --rm --network none --read-only --user "$(id -u):$(id -g)" --cap-drop ALL \
      --security-opt no-new-privileges:true \
      --mount "type=bind,src=$snapshot,dst=/snapshot" \
      --entrypoint python "$image" -c '
import sqlite3, sys
source = sqlite3.connect("file:" + sys.argv[1] + "?mode=ro", uri=True)
target = sqlite3.connect("/snapshot/consistent.db")
source.backup(target)
assert target.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
target.close(); source.close()
' "/snapshot/$database"
    mv "$snapshot/consistent.db" "$BACKUP_DIR/$database"
    chmod 600 "$BACKUP_DIR/$database"
    rm -rf -- "$snapshot"
    return
  fi
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

rollback() {
  echo "Opdatering fejlede. Gendanner de tidligere images." >&2
  "${GW[@]}" stop sms-gateway || true
  cp "$BACKUP_DIR/env.backup" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  "${WA[@]}" -f "$BACKUP_DIR/rollback-pager.yml" up -d --no-build --no-deps sms-whatsapp || true
  docker compose --env-file "$ENV_FILE" -f "$OLD_GW_COMPOSE" -f "$BACKUP_DIR/rollback-gateway.yml" up -d --no-build --no-deps sms-gateway || true
  if [[ "${OLD_DRIVER:-usb}" == usb && "$DRIVER" == cudy ]]; then
    echo "Rollback bruger USB igen; SMS kræver tilsluttet USB-modem og SIM." >&2
  fi
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

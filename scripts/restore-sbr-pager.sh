#!/usr/bin/env bash
# Explicit full database recovery; leaves OpenWA and SMS Gateway untouched.
set -Eeuo pipefail
umask 077
APP_DIR="${SBR_PAGER_ROOT:-/opt/SBR-Pager-Gateway}"
BACKUP_FILE="${1:?Angiv en downloadet SQLite-backup som første argument}"
[[ "${2:-}" == GENDAN ]] || { echo 'Fuld gendannelse erstatter historik og opsætning. Tilføj GENDAN som andet argument.' >&2; exit 1; }
BACKUP_FILE="$(realpath -- "$BACKUP_FILE")"
[[ -f "$BACKUP_FILE" ]] || exit 1
cd "$APP_DIR"
WA=(docker compose --env-file "$APP_DIR/.env" -f "$APP_DIR/compose/sms-whatsapp/compose.yml")
OFFSITE_MOUNT="$(sed -n 's/^SMS_WHATSAPP_OFFSITE_MOUNT=//p' "$APP_DIR/.env" | tail -1 | tr -d '\r\"\047')"
if [[ "$OFFSITE_MOUNT" == true ]]; then
  WA+=(-f "$APP_DIR/compose/sms-whatsapp/offsite-backup.yml")
fi
"${WA[@]}" config --quiet
SUDO=()
[[ "$(id -u)" == 0 ]] || SUDO=(sudo)
WAS_TIMER=false
if systemctl is-active --quiet sbr-pager-watchdog.timer; then
  WAS_TIMER=true
  "${SUDO[@]}" systemctl stop sbr-pager-watchdog.timer
fi
# Finish a watchdog already started before its timer stopped.
if systemctl is-active --quiet sbr-pager-watchdog.service; then
  "${SUDO[@]}" systemctl stop sbr-pager-watchdog.service
fi
restart_pager() {
  "${WA[@]}" up -d --no-build --no-deps sms-whatsapp || true
  if "$WAS_TIMER"; then "${SUDO[@]}" systemctl start sbr-pager-watchdog.timer || true; fi
}
trap restart_pager EXIT
"${WA[@]}" stop sms-whatsapp
# Copy into the existing stopped container's shared /data volume.
STAGED="/data/restore-input-$$.sqlite"
docker cp "$BACKUP_FILE" "sbr-sms-whatsapp:$STAGED"
"${WA[@]}" run --rm --no-deps --user 0 --entrypoint python sms-whatsapp /app/restore_db.py "$STAGED" /data/sms-whatsapp.db
"${WA[@]}" run --rm --no-deps --user 0 --entrypoint python sms-whatsapp -c 'import os,sys; os.unlink(sys.argv[1])' "$STAGED"
echo 'Database gendannet. Kontrollér Drift & backup; afsendelse er pauset i 30 minutter.'

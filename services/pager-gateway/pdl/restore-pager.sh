#!/usr/bin/env bash
set -euo pipefail

STATE_ROOT="${PAGER_STATE_ROOT:-/var/lib/racher-pager}"
BACKUP_DIR="${PAGER_BACKUP_DIR:-/var/backups/racher-pager}"
INTEGRATION_DIR="${PAGER_INTEGRATION_DIR:-/opt/racher-pager/integration}"
BACKUP_SCRIPT="$INTEGRATION_DIR/backup-pager.sh"
COMPOSE_SCRIPT="$INTEGRATION_DIR/pager-compose.sh"
LOCK_FILE="${PAGER_MAINTENANCE_LOCK:-/run/racher-pager/maintenance.lock}"
NAME="${1:-}"

if [[ "$EUID" -ne 0 ]]; then
  echo "restore-pager.sh skal køre som root via host-agenten." >&2
  exit 1
fi

if [[ ! "$NAME" =~ ^racher-pager-[0-9]{8}T[0-9]{6}Z\.tar\.gz$ ]]; then
  echo "Ugyldigt backupnavn." >&2
  exit 1
fi
ARCHIVE="$BACKUP_DIR/$NAME"
if [[ ! -f "$ARCHIVE" ]]; then
  echo "Backup findes ikke: $NAME" >&2
  exit 1
fi

# Restore, update og rollback må aldrig køre samtidig. Gateway-watchdoggen,
# system-agenten og FSK-proben respekterer samme lock og undgår DB-skrivning/restart
# mens pager.db udskiftes.
mkdir -p "$(dirname "$LOCK_FILE")"
exec 9>"$LOCK_FILE"
flock -n 9 || { echo "En update/rollback/restore kører allerede." >&2; exit 1; }

# The gateway container runs as the owner of STATE_ROOT rather than as root.
# Remember that identity before replacing files so a restore cannot accidentally
# turn pager.db/VAPID/session files into root-owned, read-only state for Gunicorn.
STATE_UID="$(stat -c '%u' "$STATE_ROOT" 2>/dev/null || echo 0)"
STATE_GID="$(stat -c '%g' "$STATE_ROOT" 2>/dev/null || echo 0)"

TMP_DIR="$(mktemp -d)"
RUNTIME_PAUSED=0
cleanup() {
  local rc=$?
  trap - EXIT
  set +e
  if [[ "$RUNTIME_PAUSED" == "1" ]]; then
    if [[ -x "$COMPOSE_SCRIPT" ]]; then
      "$COMPOSE_SCRIPT" up -d --remove-orphans >/dev/null 2>&1 || true
    else
      docker start racher-pager-gateway >/dev/null 2>&1 || true
    fi
    systemctl start racher-pager-fsk-status.timer >/dev/null 2>&1 || true
  fi
  rm -rf "$TMP_DIR"
  exit "$rc"
}
trap cleanup EXIT

# Afvis arkiver med absolutte stier eller traversal før extraction.
while IFS= read -r entry; do
  clean="${entry#./}"
  if [[ "$clean" == /* || "$clean" == *"../"* || "$clean" == ".." ]]; then
    echo "Backup indeholder en usikker sti: $entry" >&2
    exit 1
  fi
done < <(tar -tzf "$ARCHIVE")

tar -xzf "$ARCHIVE" -C "$TMP_DIR" --no-same-owner
if [[ ! -f "$TMP_DIR/data/pager.db" ]]; then
  echo "Backup mangler data/pager.db." >&2
  exit 1
fi
sqlite3 "$TMP_DIR/data/pager.db" "PRAGMA integrity_check;" | grep -qx 'ok'

# Machine credentials/configuration belong to the currently running appliance.
# Preserve the monitor key when restoring application data on the same Pi; a
# fresh Pi has no current key and therefore simply receives the backup value.
CURRENT_MONITOR_KEY=""
if [[ -f "$STATE_ROOT/pager.db" ]]; then
  CURRENT_MONITOR_KEY="$(sqlite3 "$STATE_ROOT/pager.db" "SELECT value FROM settings WHERE key='external_monitor_access_key' LIMIT 1;" 2>/dev/null || true)"
fi

# Safety-backup af den nuværende tilstand før restore.
if [[ -x "$BACKUP_SCRIPT" ]]; then
  "$BACKUP_SCRIPT"
fi

# Stop all writers we can stop independently. The long-running host-agent cannot
# be stopped here because this restore may have been spawned by that systemd
# service; it instead observes the maintenance flock and skips DB access. The FSK
# timer/service is safe to stop and systemctl waits for any active oneshot to exit.
if [[ -x "$COMPOSE_SCRIPT" ]]; then
  "$COMPOSE_SCRIPT" stop pager-gateway >/dev/null 2>&1 || true
else
  docker stop racher-pager-gateway >/dev/null 2>&1 || true
fi
systemctl stop racher-pager-fsk-status.timer >/dev/null 2>&1 || true
systemctl stop racher-pager-fsk-status.service >/dev/null 2>&1 || true
RUNTIME_PAUSED=1

# Give an already-started short SQLite write from the host agent time to commit.
# New writes are suppressed by the maintenance lock before the database swap.
sleep 2
rm -f "$STATE_ROOT/pager.db-wal" "$STATE_ROOT/pager.db-shm"
install -m 0640 "$TMP_DIR/data/pager.db" "$STATE_ROOT/pager.db"
chown "$STATE_UID:$STATE_GID" "$STATE_ROOT/pager.db"

if [[ -n "$CURRENT_MONITOR_KEY" ]]; then
  PAGER_RESTORE_DB="$STATE_ROOT/pager.db" PAGER_RESTORE_MONITOR_KEY="$CURRENT_MONITOR_KEY" python3 - <<'PY'
import os
import sqlite3

path = os.environ["PAGER_RESTORE_DB"]
key = os.environ["PAGER_RESTORE_MONITOR_KEY"]
with sqlite3.connect(path) as conn:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES ('external_monitor_access_key', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key,),
    )
PY
fi
unset CURRENT_MONITOR_KEY

for file in session-secret vapid-private.pem; do
  if [[ -f "$TMP_DIR/data/$file" ]]; then
    install -m 0600 "$TMP_DIR/data/$file" "$STATE_ROOT/$file"
    chown "$STATE_UID:$STATE_GID" "$STATE_ROOT/$file"
  fi
done
if [[ -f "$TMP_DIR/data/pdl/pdl.ini" ]]; then
  mkdir -p "$STATE_ROOT/pdl"
  chown "$STATE_UID:$STATE_GID" "$STATE_ROOT/pdl"
  install -m 0640 "$TMP_DIR/data/pdl/pdl.ini" "$STATE_ROOT/pdl/pdl.ini"
  chown "$STATE_UID:$STATE_GID" "$STATE_ROOT/pdl/pdl.ini"
fi

mkdir -p /etc/racher-pager
for file in pdl.env gateway.env network.env cloudflared.token; do
  destination="/etc/racher-pager/$file"
  # Do not roll a working Pi's deployment path, Secure-cookie flag, Wi-Fi,
  # hotspot PIN, pinned FSK device or tunnel credential back to an older value.
  # On bare-metal disaster recovery the destination is absent, so the backup
  # still supplies the complete machine configuration.
  if [[ -f "$TMP_DIR/etc/$file" && ! -e "$destination" ]]; then
    mode=0640
    [[ "$file" == "network.env" || "$file" == "cloudflared.token" ]] && mode=0600
    install -m "$mode" "$TMP_DIR/etc/$file" "$destination"
  fi
done

if [[ -x "$COMPOSE_SCRIPT" ]]; then
  "$COMPOSE_SCRIPT" up -d --remove-orphans
else
  docker start racher-pager-gateway >/dev/null
fi
systemctl reset-failed racher-pdl.service >/dev/null 2>&1 || true
systemctl restart racher-pdl.service >/dev/null 2>&1 || true
systemctl restart cloudflared.service >/dev/null 2>&1 || true
systemctl start racher-pager-fsk-status.timer >/dev/null 2>&1 || true
RUNTIME_PAUSED=0

# Agenten kan have cached timing/state around the old DB. Restart it just after
# this script releases the maintenance lock.
if command -v systemd-run >/dev/null 2>&1; then
  systemd-run --unit=racher-pager-agent-restart --on-active=3s \
    /usr/bin/systemctl restart racher-pager-system-agent.service >/dev/null 2>&1 || true
fi

echo "Backup gendannet: $NAME"

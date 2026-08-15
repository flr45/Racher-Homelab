#!/usr/bin/env bash
set -euo pipefail

STATE_ROOT="${PAGER_STATE_ROOT:-/var/lib/racher-pager}"
DB_PATH="${PAGER_DB_PATH:-$STATE_ROOT/pager.db}"
BACKUP_DIR="${PAGER_BACKUP_DIR:-/var/backups/racher-pager}"
RETENTION_DAYS="${PAGER_BACKUP_RETENTION_DAYS:-14}"
CONFIG_DIR="${PAGER_CONFIG_DIR:-/etc/racher-pager}"
LOCK_FILE="${PAGER_BACKUP_LOCK_FILE:-/run/racher-pager-backup.lock}"
TMP_DIR="$(mktemp -d)"

cleanup() { rm -rf "$TMP_DIR"; }
trap cleanup EXIT

if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "sqlite3 mangler; kan ikke lave konsistent database-backup." >&2
  exit 1
fi
if ! command -v flock >/dev/null 2>&1; then
  echo "flock mangler; kan ikke sikre eksklusiv backup." >&2
  exit 1
fi
mkdir -p "$BACKUP_DIR"
chmod 0700 "$BACKUP_DIR"

# Timer-backup og manuel admin-backup må aldrig skrive samtidig. Arkivnavnet har
# sekundopløsning, så samtidige jobs kunne ellers ramme præcis samme fil.
exec 9>"$LOCK_FILE"
flock -n 9 || { echo "En pager-backup kører allerede." >&2; exit 1; }

if [[ ! -f "$DB_PATH" ]]; then
  echo "Database mangler: $DB_PATH" >&2
  exit 1
fi

# Genbrug aldrig et eksisterende sekundbaseret navn. Det kan ske ved to meget
# hurtige sekventielle manuelle backups, selv om flock beskytter mod samtidighed.
while true; do
  STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
  ARCHIVE="$BACKUP_DIR/racher-pager-$STAMP.tar.gz"
  [[ ! -e "$ARCHIVE" ]] && break
  sleep 1
done

mkdir -p "$TMP_DIR/data"
sqlite3 "$DB_PATH" ".timeout 10000" ".backup '$TMP_DIR/data/pager.db'"

for file in session-secret vapid-private.pem; do
  [[ -f "$STATE_ROOT/$file" ]] && cp -p "$STATE_ROOT/$file" "$TMP_DIR/data/$file"
done
if [[ -f "$STATE_ROOT/pdl/pdl.ini" ]]; then
  mkdir -p "$TMP_DIR/data/pdl"
  cp -p "$STATE_ROOT/pdl/pdl.ini" "$TMP_DIR/data/pdl/pdl.ini"
fi

mkdir -p "$TMP_DIR/etc"
for file in pdl.env gateway.env network.env cloudflared.token; do
  [[ -f "$CONFIG_DIR/$file" ]] && cp -p "$CONFIG_DIR/$file" "$TMP_DIR/etc/$file"
done

cat > "$TMP_DIR/backup-info.txt" <<EOF
created_utc=$STAMP
hostname=$(hostname)
database=$DB_PATH
state_root=$STATE_ROOT
EOF

tar -C "$TMP_DIR" -czf "$ARCHIVE" .
chmod 0600 "$ARCHIVE"
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'racher-pager-*.tar.gz' \
  -mtime "+$RETENTION_DAYS" -delete

echo "Backup oprettet: $ARCHIVE"

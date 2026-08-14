#!/usr/bin/env bash
set -euo pipefail

STATE_ROOT="${PAGER_STATE_ROOT:-/var/lib/racher-pager}"
BACKUP_DIR="${PAGER_BACKUP_DIR:-/var/backups/racher-pager}"
INTEGRATION_DIR="${PAGER_INTEGRATION_DIR:-/opt/racher-pager/integration}"
BACKUP_SCRIPT="$INTEGRATION_DIR/backup-pager.sh"
COMPOSE_SCRIPT="$INTEGRATION_DIR/pager-compose.sh"
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

TMP_DIR="$(mktemp -d)"
cleanup() { rm -rf "$TMP_DIR"; }
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

# Safety-backup af den nuværende tilstand før restore.
if [[ -x "$BACKUP_SCRIPT" ]]; then
  "$BACKUP_SCRIPT"
fi

if [[ -x "$COMPOSE_SCRIPT" ]]; then
  "$COMPOSE_SCRIPT" stop pager-gateway >/dev/null 2>&1 || true
else
  docker stop racher-pager-gateway >/dev/null 2>&1 || true
fi

install -m 0640 "$TMP_DIR/data/pager.db" "$STATE_ROOT/pager.db"
for file in session-secret vapid-private.pem; do
  if [[ -f "$TMP_DIR/data/$file" ]]; then
    install -m 0600 "$TMP_DIR/data/$file" "$STATE_ROOT/$file"
  fi
done
if [[ -f "$TMP_DIR/data/pdl/pdl.ini" ]]; then
  mkdir -p "$STATE_ROOT/pdl"
  install -m 0640 "$TMP_DIR/data/pdl/pdl.ini" "$STATE_ROOT/pdl/pdl.ini"
fi

mkdir -p /etc/racher-pager
for file in pdl.env gateway.env network.env cloudflared.token; do
  if [[ -f "$TMP_DIR/etc/$file" ]]; then
    mode=0640
    [[ "$file" == "network.env" || "$file" == "cloudflared.token" ]] && mode=0600
    install -m "$mode" "$TMP_DIR/etc/$file" "/etc/racher-pager/$file"
  fi
done

if [[ -x "$COMPOSE_SCRIPT" ]]; then
  "$COMPOSE_SCRIPT" up -d --remove-orphans
else
  docker start racher-pager-gateway >/dev/null
fi
systemctl restart racher-pdl.service >/dev/null 2>&1 || true
systemctl restart cloudflared.service >/dev/null 2>&1 || true

# Agenten kan have fået ny DB/config. Genstart den efter dette script er afsluttet.
if command -v systemd-run >/dev/null 2>&1; then
  systemd-run --unit=racher-pager-agent-restart --on-active=3s \
    /usr/bin/systemctl restart racher-pager-system-agent.service >/dev/null 2>&1 || true
fi

echo "Backup gendannet: $NAME"

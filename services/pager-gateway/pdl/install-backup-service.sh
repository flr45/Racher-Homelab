#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_ROOT="/opt/racher-pager/integration"
STATE_ROOT="${PAGER_STATE_ROOT:-/var/lib/racher-pager}"
BACKUP_DIR="${PAGER_BACKUP_DIR:-/var/backups/racher-pager}"
SERVICE_FILE="/etc/systemd/system/racher-pager-backup.service"
TIMER_FILE="/etc/systemd/system/racher-pager-backup.timer"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "Backup-servicen kan kun installeres på Linux/Raspberry Pi." >&2
  exit 1
fi

sudo mkdir -p "$INSTALL_ROOT" "$STATE_ROOT" "$BACKUP_DIR"
sudo chmod 0700 "$BACKUP_DIR"
sudo install -m 0755 "$SCRIPT_DIR/backup-pager.sh" "$INSTALL_ROOT/backup-pager.sh"

sudo tee "$SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=Racher Pager secure local backup
After=local-fs.target

[Service]
Type=oneshot
User=root
Environment=PAGER_STATE_ROOT=$STATE_ROOT
Environment=PAGER_DB_PATH=$STATE_ROOT/pager.db
Environment=PAGER_BACKUP_DIR=$BACKUP_DIR
ExecStart=$INSTALL_ROOT/backup-pager.sh
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=full
ReadWritePaths=$STATE_ROOT $BACKUP_DIR
ReadOnlyPaths=/etc/racher-pager
EOF

sudo tee "$TIMER_FILE" >/dev/null <<'EOF'
[Unit]
Description=Daily Racher Pager backup

[Timer]
OnCalendar=*-*-* 03:15:00
Persistent=true
RandomizedDelaySec=300
Unit=racher-pager-backup.service

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now racher-pager-backup.timer

# Opret en første backup med det samme, så installationen kan valideres nu.
sudo systemctl start racher-pager-backup.service

echo "Daglig backup er installeret."
echo "Backupmappe: $BACKUP_DIR"
echo "Timer: sudo systemctl status racher-pager-backup.timer --no-pager"

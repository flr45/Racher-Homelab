#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID}" -eq 0 ]]; then
  echo "Kør scriptet som din normale bruger, ikke som root."
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="$USER"
SERVICE_GROUP="$(id -gn)"
SERVICE_PATH="/etc/systemd/system/racher-homelab-backup.service"
TIMER_PATH="/etc/systemd/system/racher-homelab-backup.timer"

[[ -x "$ROOT/scripts/backup.sh" ]] || chmod +x "$ROOT/scripts/backup.sh"
[[ -f "$ROOT/.env" ]] || {
  echo "Mangler $ROOT/.env"
  exit 1
}

sudo tee "$SERVICE_PATH" >/dev/null <<EOF
[Unit]
Description=Racher HomeLab backup
Requires=docker.service
After=docker.service network-online.target

[Service]
Type=oneshot
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$ROOT
Environment=ENV_FILE=$ROOT/.env
ExecStart=$ROOT/scripts/backup.sh
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7

[Install]
WantedBy=multi-user.target
EOF

sudo tee "$TIMER_PATH" >/dev/null <<'EOF'
[Unit]
Description=Kør Racher HomeLab backup hver nat

[Timer]
OnCalendar=*-*-* 03:15:00
Persistent=true
RandomizedDelaySec=10m
Unit=racher-homelab-backup.service

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now racher-homelab-backup.timer

echo
echo "Automatisk backup er installeret."
echo "Næste kørsel:"
systemctl list-timers racher-homelab-backup.timer --no-pager

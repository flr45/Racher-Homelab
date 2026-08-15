#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
  echo "Kør scriptet som normal bruger; det bruger selv sudo." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:-racher-pi2}"
BASE_URL="${PAGER_MONITOR_URL:-http://$TARGET:8088}"
INSTALL_DIR="/opt/racher-pager-monitor"
STATE_DIR="/var/lib/racher-pager-monitor"
ENV_DIR="/etc/racher-pager"
ENV_FILE="$ENV_DIR/external-monitor.env"
UNIT_FILE="/etc/systemd/system/racher-pager-external-monitor.service"
TIMER_FILE="/etc/systemd/system/racher-pager-external-monitor.timer"

command -v tailscale >/dev/null 2>&1 || { echo "Tailscale mangler på denne Pi." >&2; exit 1; }
[[ -f "$SCRIPT_DIR/external_monitor.py" ]] || { echo "Mangler external_monitor.py" >&2; exit 1; }

sudo mkdir -p "$INSTALL_DIR" "$STATE_DIR" "$ENV_DIR"
sudo install -m 0755 "$SCRIPT_DIR/external_monitor.py" "$INSTALL_DIR/external_monitor.py"
sudo chmod 0750 "$STATE_DIR"

if [[ ! -f "$ENV_FILE" ]]; then
  sudo tee "$ENV_FILE" >/dev/null <<EOF
PAGER_MONITOR_TARGET=$TARGET
PAGER_MONITOR_URL=$BASE_URL
PAGER_MONITOR_SMS_GATEWAY=http://127.0.0.1:8090
PAGER_MONITOR_STATE_FILE=$STATE_DIR/state.json
EOF
  sudo chmod 0640 "$ENV_FILE"
else
  echo "Beholder eksisterende konfiguration: $ENV_FILE"
fi

sudo tee "$UNIT_FILE" >/dev/null <<EOF
[Unit]
Description=External health monitor for Racher Pager Gateway
After=network-online.target tailscaled.service docker.service
Wants=network-online.target tailscaled.service

[Service]
Type=oneshot
User=root
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=-$ENV_FILE
ExecStart=/usr/bin/python3 $INSTALL_DIR/external_monitor.py
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=$STATE_DIR
EOF

sudo tee "$TIMER_FILE" >/dev/null <<'EOF'
[Unit]
Description=Check Racher Pager Gateway every two minutes

[Timer]
OnBootSec=90s
OnUnitActiveSec=2min
AccuracySec=5s
Persistent=true
Unit=racher-pager-external-monitor.service

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now racher-pager-external-monitor.timer

echo "Ekstern Pager-monitor installeret."
echo "Target: $TARGET"
echo "Gateway: $BASE_URL"
echo "SMS gateway: http://127.0.0.1:8090"
echo "Timer: hvert 2. minut"
echo "Status: sudo systemctl status racher-pager-external-monitor.timer --no-pager"

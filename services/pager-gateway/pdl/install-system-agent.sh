#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
SERVICE_DIR="$REPO_ROOT/services/pager-gateway"
DATA_DIR="${PAGER_DATA_HOST_PATH:-/var/lib/racher-pager}"
UNIT_PATH="/etc/systemd/system/racher-pager-system-agent.service"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "System-agenten installeres kun på Linux/Raspberry Pi." >&2
  exit 1
fi

sudo mkdir -p "$DATA_DIR"
sudo touch "$DATA_DIR/pager.db"
sudo chmod 0750 "$DATA_DIR"

if [[ ! -f "$SERVICE_DIR/system_agent.py" ]]; then
  echo "Mangler $SERVICE_DIR/system_agent.py" >&2
  exit 1
fi

sudo tee "$UNIT_PATH" >/dev/null <<EOF
[Unit]
Description=Racher Pager privileged system action agent
After=docker.service racher-pdl.service
Wants=docker.service

[Service]
Type=simple
User=root
WorkingDirectory=$SERVICE_DIR
Environment=PAGER_DB_PATH=$DATA_DIR/pager.db
ExecStart=/usr/bin/python3 $SERVICE_DIR/system_agent.py
Restart=always
RestartSec=3
NoNewPrivileges=true
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now racher-pager-system-agent.service

echo "Racher Pager system-agent er installeret."
echo "Status: sudo systemctl status racher-pager-system-agent --no-pager"

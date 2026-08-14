#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
SERVICE_DIR="$REPO_ROOT/services/pager-gateway"
DATA_DIR="${PAGER_DATA_HOST_PATH:-/var/lib/racher-pager}"
INSTALL_DIR="${PAGER_SYSTEM_AGENT_INSTALL_DIR:-/opt/racher-pager/system-agent}"
BACKUP_DIR="${PAGER_BACKUP_DIR:-/var/backups/racher-pager}"
UNIT_PATH="/etc/systemd/system/racher-pager-system-agent.service"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "System-agenten installeres kun på Linux/Raspberry Pi." >&2
  exit 1
fi

for required in system_agent.py storage.py; do
  if [[ ! -f "$SERVICE_DIR/$required" ]]; then
    echo "Mangler $SERVICE_DIR/$required" >&2
    exit 1
  fi
done

sudo mkdir -p "$DATA_DIR" "$INSTALL_DIR" "$BACKUP_DIR"
sudo touch "$DATA_DIR/pager.db"
sudo chmod 0750 "$DATA_DIR"
sudo chmod 0700 "$BACKUP_DIR"

# Kopiér agenten væk fra git-checkoutet, så servicen ikke afhænger af hvor
# repository-mappen senere ligger eller hvilken branch der er checket ud.
sudo install -m 0755 "$SERVICE_DIR/system_agent.py" "$INSTALL_DIR/system_agent.py"
sudo install -m 0644 "$SERVICE_DIR/storage.py" "$INSTALL_DIR/storage.py"

sudo tee "$UNIT_PATH" >/dev/null <<EOF
[Unit]
Description=Racher Pager privileged system action and health agent
After=docker.service racher-pdl.service local-fs.target
Wants=docker.service

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
Environment=PAGER_DB_PATH=$DATA_DIR/pager.db
Environment=PAGER_STATE_ROOT=$DATA_DIR
Environment=PDL_CONFIG_PATH=$DATA_DIR/pdl/pdl.ini
Environment=PDL_LOG_PATH=$DATA_DIR/pdl.log
Environment=PAGER_BACKUP_DIR=$BACKUP_DIR
ExecStart=/usr/bin/python3 $INSTALL_DIR/system_agent.py
Restart=always
RestartSec=3
NoNewPrivileges=true
ProtectHome=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=$DATA_DIR $BACKUP_DIR

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now racher-pager-system-agent.service

echo "Racher Pager system-agent er installeret."
echo "Installeret kode: $INSTALL_DIR"
echo "Status: sudo systemctl status racher-pager-system-agent --no-pager"

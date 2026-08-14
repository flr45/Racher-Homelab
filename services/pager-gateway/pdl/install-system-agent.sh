#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
SERVICE_DIR="$REPO_ROOT/services/pager-gateway"
DATA_DIR="${PAGER_DATA_HOST_PATH:-/var/lib/racher-pager}"
AGENT_DIR="${PAGER_SYSTEM_AGENT_INSTALL_DIR:-/opt/racher-pager/system-agent}"
INTEGRATION_DIR="${PAGER_INTEGRATION_DIR:-/opt/racher-pager/integration}"
BACKUP_DIR="${PAGER_BACKUP_DIR:-/var/backups/racher-pager}"
RUNTIME_REPO="${PAGER_RUNTIME_REPO:-/opt/racher-pager/runtime-repo}"
UNIT_PATH="/etc/systemd/system/racher-pager-system-agent.service"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "System-agenten installeres kun på Linux/Raspberry Pi." >&2
  exit 1
fi

for required in system_agent.py storage.py; do
  [[ -f "$SERVICE_DIR/$required" ]] || { echo "Mangler $SERVICE_DIR/$required" >&2; exit 1; }
done
for required in backup-pager.sh restore-pager.sh update-pager.sh rollback-pager.sh pager-compose.sh; do
  [[ -f "$SERVICE_DIR/pdl/$required" ]] || { echo "Mangler $SERVICE_DIR/pdl/$required" >&2; exit 1; }
done

sudo mkdir -p "$DATA_DIR" "$AGENT_DIR" "$INTEGRATION_DIR" "$BACKUP_DIR" "$DATA_DIR/update"
sudo touch "$DATA_DIR/pager.db"
sudo chmod 0750 "$DATA_DIR"
sudo chmod 0700 "$BACKUP_DIR"

sudo install -m 0755 "$SERVICE_DIR/system_agent.py" "$AGENT_DIR/system_agent.py"
sudo install -m 0644 "$SERVICE_DIR/storage.py" "$AGENT_DIR/storage.py"
for helper in backup-pager.sh restore-pager.sh update-pager.sh rollback-pager.sh pager-compose.sh; do
  sudo install -m 0755 "$SERVICE_DIR/pdl/$helper" "$INTEGRATION_DIR/$helper"
done

sudo tee "$UNIT_PATH" >/dev/null <<EOF
[Unit]
Description=Racher Pager privileged system action and health agent
After=docker.service racher-pdl.service NetworkManager.service local-fs.target
Wants=docker.service NetworkManager.service

[Service]
Type=simple
User=root
WorkingDirectory=$AGENT_DIR
Environment=PAGER_DB_PATH=$DATA_DIR/pager.db
Environment=PAGER_STATE_ROOT=$DATA_DIR
Environment=PDL_CONFIG_PATH=$DATA_DIR/pdl/pdl.ini
Environment=PDL_LOG_PATH=$DATA_DIR/pdl.log
Environment=PAGER_BACKUP_DIR=$BACKUP_DIR
Environment=PAGER_INTEGRATION_DIR=$INTEGRATION_DIR
Environment=PAGER_RUNTIME_REPO=$RUNTIME_REPO
EnvironmentFile=-/etc/racher-pager/gateway.env
EnvironmentFile=-/etc/racher-pager/network.env
ExecStart=/usr/bin/python3 $AGENT_DIR/system_agent.py
Restart=always
RestartSec=3
NoNewPrivileges=true
ProtectHome=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=$DATA_DIR $BACKUP_DIR $RUNTIME_REPO $AGENT_DIR $INTEGRATION_DIR /etc/racher-pager /etc/systemd/system

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now racher-pager-system-agent.service

echo "Racher Pager system-agent er installeret."
echo "Agentkode: $AGENT_DIR"
echo "Helpers:   $INTEGRATION_DIR"
echo "Status: sudo systemctl status racher-pager-system-agent --no-pager"

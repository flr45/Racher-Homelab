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
FSK_UNIT_PATH="/etc/systemd/system/racher-pager-fsk-status.service"
FSK_TIMER_PATH="/etc/systemd/system/racher-pager-fsk-status.timer"
WATCHDOG_UNIT_PATH="/etc/systemd/system/racher-pager-gateway-watchdog.service"
WATCHDOG_TIMER_PATH="/etc/systemd/system/racher-pager-gateway-watchdog.timer"
HARDWARE_WATCHDOG_CONF="/etc/systemd/system.conf.d/racher-pager-watchdog.conf"
WATCHDOG_RUNTIME_DIR="/run/racher-pager"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "System-agenten installeres kun på Linux/Raspberry Pi." >&2
  exit 1
fi

for required in system_agent.py fsk_status_agent.py gateway_watchdog.py storage.py; do
  [[ -f "$SERVICE_DIR/$required" ]] || { echo "Mangler $SERVICE_DIR/$required" >&2; exit 1; }
done
for required in backup-pager.sh restore-pager.sh update-pager.sh rollback-pager.sh pager-compose.sh; do
  [[ -f "$SERVICE_DIR/pdl/$required" ]] || { echo "Mangler $SERVICE_DIR/pdl/$required" >&2; exit 1; }
done

sudo mkdir -p "$DATA_DIR" "$AGENT_DIR" "$INTEGRATION_DIR" "$BACKUP_DIR" "$DATA_DIR/update" "$WATCHDOG_RUNTIME_DIR"
sudo touch "$DATA_DIR/pager.db"
sudo chmod 0750 "$DATA_DIR"
sudo chmod 0700 "$BACKUP_DIR"
sudo chmod 0755 "$WATCHDOG_RUNTIME_DIR"

sudo install -m 0755 "$SERVICE_DIR/system_agent.py" "$AGENT_DIR/system_agent.py"
sudo install -m 0755 "$SERVICE_DIR/fsk_status_agent.py" "$AGENT_DIR/fsk_status_agent.py"
sudo install -m 0755 "$SERVICE_DIR/gateway_watchdog.py" "$AGENT_DIR/gateway_watchdog.py"
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

sudo tee "$FSK_UNIT_PATH" >/dev/null <<EOF
[Unit]
Description=Racher Pager FSK-USB hardware status probe
After=local-fs.target racher-pdl.service

[Service]
Type=oneshot
User=root
WorkingDirectory=$AGENT_DIR
Environment=PAGER_DB_PATH=$DATA_DIR/pager.db
Environment=PDL_CONFIG_PATH=$DATA_DIR/pdl/pdl.ini
EnvironmentFile=-/etc/racher-pager/pdl.env
ExecStart=/usr/bin/python3 $AGENT_DIR/fsk_status_agent.py
NoNewPrivileges=true
ProtectHome=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$DATA_DIR
EOF

sudo tee "$FSK_TIMER_PATH" >/dev/null <<'EOF'
[Unit]
Description=Poll Racher Pager FSK-USB hardware status

[Timer]
OnBootSec=5s
OnUnitActiveSec=10s
AccuracySec=1s
Unit=racher-pager-fsk-status.service

[Install]
WantedBy=timers.target
EOF

# Docker's restart policy handles process exits, but not a wedged Gunicorn process
# that remains alive. This independent root-owned watchdog probes /healthz and
# restarts only after three consecutive failures. It shares the update/restore
# maintenance lock, so planned downtime never triggers an accidental restart.
sudo tee "$WATCHDOG_UNIT_PATH" >/dev/null <<EOF
[Unit]
Description=Racher Pager Gateway health watchdog
After=docker.service network.target
Wants=docker.service

[Service]
Type=oneshot
User=root
WorkingDirectory=$AGENT_DIR
RuntimeDirectory=racher-pager
RuntimeDirectoryMode=0755
RuntimeDirectoryPreserve=yes
Environment=PAGER_RUNTIME_DIR=$WATCHDOG_RUNTIME_DIR
Environment=PAGER_WATCHDOG_STATE_FILE=$WATCHDOG_RUNTIME_DIR/gateway-watchdog.failures
Environment=PAGER_MAINTENANCE_LOCK=$WATCHDOG_RUNTIME_DIR/maintenance.lock
Environment=PAGER_WATCHDOG_FAILURE_THRESHOLD=3
EnvironmentFile=-/etc/racher-pager/gateway.env
ExecStart=/usr/bin/python3 $AGENT_DIR/gateway_watchdog.py
NoNewPrivileges=true
ProtectHome=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$WATCHDOG_RUNTIME_DIR
EOF

sudo tee "$WATCHDOG_TIMER_PATH" >/dev/null <<'EOF'
[Unit]
Description=Poll Racher Pager Gateway health

[Timer]
OnBootSec=45s
OnUnitActiveSec=20s
AccuracySec=2s
Unit=racher-pager-gateway-watchdog.service

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now racher-pager-system-agent.service
sudo systemctl enable --now racher-pager-fsk-status.timer
sudo systemctl enable --now racher-pager-gateway-watchdog.timer

# A process-level watchdog cannot recover a completely frozen Linux userspace or
# kernel. Raspberry Pi exposes a hardware watchdog on supported installations.
# When available, let systemd feed it; if systemd itself stops scheduling for
# roughly 45 seconds the hardware reboots the Pi automatically.
sudo modprobe bcm2835_wdt >/dev/null 2>&1 || true
if [[ -e /dev/watchdog || -e /dev/watchdog0 ]]; then
  sudo mkdir -p "$(dirname "$HARDWARE_WATCHDOG_CONF")"
  sudo tee "$HARDWARE_WATCHDOG_CONF" >/dev/null <<'EOF'
[Manager]
RuntimeWatchdogSec=45s
EOF
  sudo systemctl daemon-reexec
  HARDWARE_WATCHDOG_STATUS="aktiv (45 sek.)"
else
  HARDWARE_WATCHDOG_STATUS="afventer understøttet /dev/watchdog"
fi

echo "Racher Pager system-agent er installeret."
echo "Agentkode:       $AGENT_DIR"
echo "Helpers:         $INTEGRATION_DIR"
echo "FSK probe:       racher-pager-fsk-status.timer (10 sek.)"
echo "Gateway watchdog: racher-pager-gateway-watchdog.timer (20 sek., 3 fejl)"
echo "Pi watchdog:     $HARDWARE_WATCHDOG_STATUS"
echo "Status: sudo systemctl status racher-pager-system-agent --no-pager"

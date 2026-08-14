#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="${PDL_RUN_USER:-$(id -un)}"
RUN_GROUP="${PDL_RUN_GROUP:-$(id -gn)}"
STATE_ROOT="${PAGER_STATE_ROOT:-/var/lib/racher-pager}"
PDL_STATE_DIR="${PDL_STATE_DIR:-$STATE_ROOT/pdl}"
ENV_DIR="/etc/racher-pager"
ENV_FILE="$ENV_DIR/pdl.env"
UNIT_FILE="/etc/systemd/system/racher-pdl.service"
INSTALL_ROOT="/opt/racher-pager/integration"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "systemd-servicen kan kun installeres på Linux." >&2
  exit 1
fi

sudo mkdir -p "$STATE_ROOT" "$PDL_STATE_DIR" "$ENV_DIR" "$INSTALL_ROOT"
sudo chown -R "$RUN_USER:$RUN_GROUP" "$STATE_ROOT"

sudo install -m 0755 "$SCRIPT_DIR/configure-pdl.sh" "$INSTALL_ROOT/configure-pdl.sh"
sudo install -m 0755 "$SCRIPT_DIR/run-pdl-headless.sh" "$INSTALL_ROOT/run-pdl-headless.sh"

if [[ ! -f "$ENV_FILE" ]]; then
  sudo tee "$ENV_FILE" >/dev/null <<'EOF'
# ALSA input. Start med "default"; find USB-kort med: arecord -L
PDL_CAPTURE_DEVICE=default
PDL_SAMPLE_RATE=48000
PDL_BAUD_512=1
PDL_BAUD_1200=1
PDL_BAUD_2400=1
PDL_INVERT=0
PAGER_STATE_ROOT=/var/lib/racher-pager
EOF
  sudo chmod 0640 "$ENV_FILE"
fi

sudo tee "$UNIT_FILE" >/dev/null <<EOF
[Unit]
Description=Racher PDL POCSAG Decoder
After=local-fs.target sound.target
Wants=sound.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_GROUP
SupplementaryGroups=audio
WorkingDirectory=$PDL_STATE_DIR
EnvironmentFile=-$ENV_FILE
ExecStartPre=$INSTALL_ROOT/configure-pdl.sh
ExecStart=$INSTALL_ROOT/run-pdl-headless.sh
Restart=always
RestartSec=2
TimeoutStopSec=15
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=$STATE_ROOT

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable racher-pdl.service

echo "PDL systemd-service er installeret og aktiveret."
echo "Konfiguration: $ENV_FILE"
echo "Start:  sudo systemctl start racher-pdl"
echo "Status: sudo systemctl status racher-pdl --no-pager"
echo "Log:    journalctl -u racher-pdl -f"

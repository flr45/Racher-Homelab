#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="${PDL_RUN_USER:-$(id -un)}"
RUN_GROUP="${PDL_RUN_GROUP:-$(id -gn)}"
STATE_ROOT="${PAGER_STATE_ROOT:-/var/lib/racher-pager}"
PDL_STATE_DIR="${PDL_STATE_DIR:-$STATE_ROOT/pdl}"
PDL_LOG_PATH="${PDL_LOG_PATH:-$STATE_ROOT/pdl.log}"
ENV_DIR="/etc/racher-pager"
ENV_FILE="$ENV_DIR/pdl.env"
UNIT_FILE="/etc/systemd/system/racher-pdl.service"
LOGROTATE_FILE="/etc/logrotate.d/racher-pager-pdl"
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
# Primært input: discriminator.nl FSK->USB (FTDI serial bitstream).
# Lad PDL_RS232_DEVICE stå tom ved første boot. Wrapperen foretrækker automatisk
# /dev/serial/by-id/* og falder derefter tilbage til /dev/ttyUSB* /dev/ttyACM*.
# Når den konkrete FTDI-enhed er kendt, kan vi pinne dens by-id sti her.
PDL_INPUT_MODE=fsk-usb
PDL_RS232_DEVICE=
PDL_RS232_PORT=1
PDL_RS232_BITRATE=19200
# Legacy PDW timing mode: 1=POCSAG, 2=FLEX 1600, 3=Mobitex 8000.
PDL_RS232_DECODE_MODE=1
# Start POCSAG-1200 sync search earlier than legacy PDW's >180 preamble count.
# Runtime patch clamps this value to 60..180. Live Vordingborg reached 166.
PDL_POCSAG_1200_ACQUIRE_THRESHOLD=120

# POCSAG-rater som PDL må forsøge at dekode. DIP-switch på FSK-USB skal samtidig stå korrekt.
PDL_BAUD_512=1
PDL_BAUD_1200=1
PDL_BAUD_2400=1

# Audio/ALSA beholdes som fallback og kan aktiveres med PDL_INPUT_MODE=audio.
PDL_CAPTURE_DEVICE=default
PDL_SAMPLE_RATE=48000
PDL_INVERT=0
PAGER_STATE_ROOT=/var/lib/racher-pager
EOF
  sudo chmod 0640 "$ENV_FILE"
fi

# Early Pager Gateway builds incorrectly seeded mode 2 (FLEX 1600 timing) for
# the POCSAG FSK-USB path. Migrate only that exact legacy default; preserve any
# other explicit operator value.
if sudo grep -qx 'PDL_RS232_DECODE_MODE=2' "$ENV_FILE" 2>/dev/null; then
  sudo sed -i 's/^PDL_RS232_DECODE_MODE=2$/PDL_RS232_DECODE_MODE=1/' "$ENV_FILE"
fi

# Existing appliances predate the configurable acquisition threshold. Add the
# measured-safe default once without overwriting a later operator tuning value.
if ! sudo grep -q '^PDL_POCSAG_1200_ACQUIRE_THRESHOLD=' "$ENV_FILE" 2>/dev/null; then
  printf '\n# POCSAG-1200 preamble acquisition (clamped 60..180)\nPDL_POCSAG_1200_ACQUIRE_THRESHOLD=120\n' \
    | sudo tee -a "$ENV_FILE" >/dev/null
fi

# PDL writes a persistent diagnostic stream. Keep enough history for reception
# troubleshooting without allowing a long-lived appliance to fill its SD/SSD.
# copytruncate is intentional: PDL keeps the output file descriptor open, and the
# gateway tailer already detects truncation safely.
sudo tee "$LOGROTATE_FILE" >/dev/null <<EOF
$PDL_LOG_PATH {
    daily
    rotate 30
    maxsize 20M
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    su $RUN_USER $RUN_GROUP
}
EOF
sudo chmod 0644 "$LOGROTATE_FILE"

sudo tee "$UNIT_FILE" >/dev/null <<EOF
[Unit]
Description=Racher PDL POCSAG Decoder
After=local-fs.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_GROUP
SupplementaryGroups=audio dialout
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
echo "Standardinput: FSK-USB / RS232 19200 8N1"
echo "POCSAG-1200 acquisition: PDL_POCSAG_1200_ACQUIRE_THRESHOLD (standard 120)"
echo "PDL-log: $PDL_LOG_PATH · roteres dagligt/ved 20 MB · 30 rotationer"
echo "Start:  sudo systemctl start racher-pdl"
echo "Status: sudo systemctl status racher-pdl --no-pager"
echo "Log:    journalctl -u racher-pdl -f"

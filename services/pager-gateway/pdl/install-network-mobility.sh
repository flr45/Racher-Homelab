#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
SERVICE_DIR="$REPO_ROOT/services/pager-gateway"
ENV_DIR="/etc/racher-pager"
NETWORK_ENV="$ENV_DIR/network.env"
INSTALL_DIR="/opt/racher-pager/network"
PORTAL_UNIT="/etc/systemd/system/racher-pager-network-portal.service"
HOTSPOT_CONNECTION="${PAGER_HOTSPOT_CONNECTION:-Racher-Pager-Setup}"
HOTSPOT_SSID="${PAGER_HOTSPOT_SSID:-Racher-Pager-Setup}"
HOTSPOT_IP="${PAGER_HOTSPOT_IP:-10.42.0.1}"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "Netværks-mobility installeres kun på Linux/Raspberry Pi." >&2
  exit 1
fi

sudo apt-get update
sudo apt-get install -y network-manager
sudo systemctl enable --now NetworkManager.service

WIFI_IFACE="${PAGER_WIFI_IFACE:-}"
if [[ -z "$WIFI_IFACE" ]]; then
  WIFI_IFACE="$(nmcli -t -f DEVICE,TYPE device status | awk -F: '$2=="wifi" {print $1; exit}')"
fi
WIFI_IFACE="${WIFI_IFACE:-wlan0}"

sudo mkdir -p "$ENV_DIR" "$INSTALL_DIR"
if [[ ! -f "$NETWORK_ENV" ]]; then
  HOTSPOT_PASSWORD="$(python3 - <<'PY'
import secrets, string
alphabet = string.ascii_letters + string.digits
print(''.join(secrets.choice(alphabet) for _ in range(16)))
PY
)"
  sudo tee "$NETWORK_ENV" >/dev/null <<EOF
PAGER_WIFI_IFACE=$WIFI_IFACE
PAGER_HOTSPOT_CONNECTION=$HOTSPOT_CONNECTION
PAGER_HOTSPOT_SSID=$HOTSPOT_SSID
PAGER_HOTSPOT_PASSWORD=$HOTSPOT_PASSWORD
PAGER_HOTSPOT_IP=$HOTSPOT_IP
PAGER_HOTSPOT_PORTAL_PORT=80
PAGER_HOTSPOT_FALLBACK_SECONDS=180
PAGER_HOTSPOT_CYCLE_SECONDS=900
EOF
  sudo chmod 0600 "$NETWORK_ENV"
fi

# shellcheck disable=SC1090
set -a
source <(sudo cat "$NETWORK_ENV")
set +a

if ! nmcli -g NAME connection show "$PAGER_HOTSPOT_CONNECTION" >/dev/null 2>&1; then
  sudo nmcli connection add \
    type wifi ifname "$PAGER_WIFI_IFACE" \
    con-name "$PAGER_HOTSPOT_CONNECTION" ssid "$PAGER_HOTSPOT_SSID"
fi

sudo nmcli connection modify "$PAGER_HOTSPOT_CONNECTION" \
  connection.autoconnect no \
  802-11-wireless.mode ap \
  802-11-wireless.band bg \
  ipv4.method shared \
  ipv4.addresses "$PAGER_HOTSPOT_IP/24" \
  ipv6.method disabled \
  wifi-sec.key-mgmt wpa-psk \
  wifi-sec.psk "$PAGER_HOTSPOT_PASSWORD"

sudo install -m 0755 "$SERVICE_DIR/network_portal.py" "$INSTALL_DIR/network_portal.py"

sudo tee "$PORTAL_UNIT" >/dev/null <<EOF
[Unit]
Description=Racher Pager fallback Wi-Fi setup portal
After=NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=simple
User=root
EnvironmentFile=$NETWORK_ENV
ExecStart=/usr/bin/python3 $INSTALL_DIR/network_portal.py
Restart=always
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=full

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now racher-pager-network-portal.service

cat <<EOF
Wi-Fi mobility er installeret.
Fallback SSID : $PAGER_HOTSPOT_SSID
Setup portal  : http://$PAGER_HOTSPOT_IP/
Password/PIN  : $PAGER_HOTSPOT_PASSWORD

Gem Password/PIN et sikkert sted. Hotspottet starter kun, når system-agenten har været uden internet i fallback-perioden, eller når admin starter det manuelt.
EOF

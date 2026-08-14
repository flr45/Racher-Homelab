#!/usr/bin/env bash
set -euo pipefail

ENV_DIR="/etc/racher-pager"
TOKEN_FILE="$ENV_DIR/cloudflared.token"
UNIT_FILE="/etc/systemd/system/cloudflared.service"
GATEWAY_ENV="$ENV_DIR/gateway.env"
PUBLIC_HOSTNAME="${PAGER_PUBLIC_HOSTNAME:-${2:-}}"
TOKEN="${CLOUDFLARE_TUNNEL_TOKEN:-${1:-}}"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "Cloudflare Tunnel installeres kun på Linux/Raspberry Pi." >&2
  exit 1
fi
if [[ -z "$TOKEN" ]]; then
  echo "Angiv tunnel-token via CLOUDFLARE_TUNNEL_TOKEN eller som første argument." >&2
  exit 1
fi

sudo mkdir -p --mode=0755 /usr/share/keyrings "$ENV_DIR"
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list >/dev/null
sudo apt-get update
sudo apt-get install -y cloudflared

printf '%s\n' "$TOKEN" | sudo tee "$TOKEN_FILE" >/dev/null
sudo chmod 0600 "$TOKEN_FILE"
unset TOKEN CLOUDFLARE_TUNNEL_TOKEN

sudo tee "$UNIT_FILE" >/dev/null <<EOF
[Unit]
Description=Cloudflare Tunnel for Racher Pager Gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
ExecStart=/usr/bin/cloudflared tunnel run --token-file $TOKEN_FILE
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectHome=true
PrivateTmp=true
ProtectSystem=full
ReadOnlyPaths=$TOKEN_FILE

[Install]
WantedBy=multi-user.target
EOF

if [[ -n "$PUBLIC_HOSTNAME" && -f "$GATEWAY_ENV" ]]; then
  sudo sed -i '/^PAGER_PUBLIC_HOSTNAME=/d' "$GATEWAY_ENV"
  echo "PAGER_PUBLIC_HOSTNAME=$PUBLIC_HOSTNAME" | sudo tee -a "$GATEWAY_ENV" >/dev/null
fi

sudo systemctl daemon-reload
sudo systemctl enable --now cloudflared.service

echo "Cloudflare Tunnel er installeret som systemd-service."
echo "Status: sudo systemctl status cloudflared --no-pager"
if [[ -n "$PUBLIC_HOSTNAME" ]]; then
  echo "Offentligt hostname registreret lokalt: $PUBLIC_HOSTNAME"
fi
echo "I Cloudflare skal tunnelens public hostname pege på http://localhost:8088."

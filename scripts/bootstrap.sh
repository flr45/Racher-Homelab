#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID}" -eq 0 ]]; then
  echo "Kør scriptet som din normale bruger, ikke som root."
  exit 1
fi

sudo apt-get update
sudo apt-get full-upgrade -y
sudo apt-get install -y ca-certificates curl git ufw unattended-upgrades

if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi

sudo usermod -aG docker "$USER"
sudo systemctl enable --now docker

sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable

sudo dpkg-reconfigure -f noninteractive unattended-upgrades || true

mkdir -p "$HOME/homelab/backups"

echo
echo "Bootstrap færdig. Log ud og ind igen, så Docker-gruppen virker."
echo "Klon derefter repository'et til ~/homelab/Racher-Homelab."

#!/usr/bin/env bash
set -Eeuo pipefail

log() { printf '[bootstrap] %s\n' "$*"; }
fail() { printf '[bootstrap] FEJL: %s\n' "$*" >&2; exit 1; }

[[ "${EUID}" -ne 0 ]] || fail "Kør scriptet som din normale bruger, ikke som root."
[[ "$(uname -s)" == "Linux" ]] || fail "Kun Linux understøttes."
command -v sudo >/dev/null 2>&1 || fail "sudo mangler."
command -v apt-get >/dev/null 2>&1 || fail "Denne installer kræver Debian/Raspberry Pi OS."

ARCH="$(dpkg --print-architecture)"
case "$ARCH" in
  arm64|amd64) ;;
  *) fail "Ikke-understøttet arkitektur: $ARCH. Brug 64-bit Raspberry Pi OS." ;;
esac

if [[ -r /proc/meminfo ]]; then
  MEM_KB="$(awk '/MemTotal/ {print $2}' /proc/meminfo)"
  [[ "${MEM_KB:-0}" -ge 1900000 ]] || fail "Mindst 2 GB RAM kræves."
fi

log "Opdaterer systempakker"
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get full-upgrade -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  ca-certificates curl git jq openssl ufw unattended-upgrades

if ! command -v docker >/dev/null 2>&1; then
  log "Installerer Docker Engine fra Docker-installeren"
  INSTALLER="$(mktemp)"
  trap 'rm -f "$INSTALLER"' EXIT
  curl --proto '=https' --tlsv1.2 -fsSL https://get.docker.com -o "$INSTALLER"
  sudo sh "$INSTALLER"
fi

sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"

docker compose version >/dev/null 2>&1 || fail "Docker Compose-plugin blev ikke installeret."

log "Konfigurerer firewall"
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable
sudo dpkg-reconfigure -f noninteractive unattended-upgrades || true

HOMELAB_ROOT="${HOMELAB_ROOT:-$HOME/homelab}"
install -d -m 0750 "$HOMELAB_ROOT" "$HOMELAB_ROOT/backups" "$HOMELAB_ROOT/data" "$HOMELAB_ROOT/releases"

log "Bootstrap færdig"
printf '\nLog ud og ind igen, så Docker-gruppen aktiveres.\n'
printf 'Kør derefter: ./scripts/install-racher-os.sh\n'

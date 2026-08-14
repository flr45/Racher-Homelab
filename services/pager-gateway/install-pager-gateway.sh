#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
  echo "Kør scriptet som din normale bruger, ikke med sudo. Scriptet bruger selv sudo hvor det er nødvendigt." >&2
  exit 1
fi
if [[ "$(uname -s)" != "Linux" ]]; then
  echo "Denne bootstrap er kun til Raspberry Pi OS/Debian/Ubuntu Linux." >&2
  exit 1
fi
if ! command -v apt-get >/dev/null 2>&1; then
  echo "Denne bootstrap kræver en apt-baseret distribution." >&2
  exit 1
fi

SOURCE_REPO="${SOURCE_REPO:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"
if [[ -z "$SOURCE_REPO" ]]; then
  echo "Kør scriptet fra et checkout af Racher-Homelab." >&2
  exit 1
fi
DEFAULT_BRANCH="$(git -C "$SOURCE_REPO" branch --show-current)"
DEFAULT_BRANCH="${DEFAULT_BRANCH:-main}"
DEPLOY_BRANCH="${PAGER_DEPLOY_BRANCH:-$DEFAULT_BRANCH}"
ORIGIN_URL="${PAGER_REPO_URL:-$(git -C "$SOURCE_REPO" config --get remote.origin.url)}"
ORIGIN_URL="${ORIGIN_URL:-https://github.com/flr45/Racher-Homelab.git}"
RUNTIME_REPO="${PAGER_RUNTIME_REPO:-/opt/racher-pager/runtime-repo}"
DEFAULT_STATE_ROOT="${PAGER_STATE_ROOT:-/var/lib/racher-pager}"
BACKUP_DIR="${PAGER_BACKUP_DIR:-/var/backups/racher-pager}"
ENV_DIR="/etc/racher-pager"
GATEWAY_ENV="$ENV_DIR/gateway.env"
DEFAULT_GATEWAY_PORT="${PAGER_GATEWAY_PORT:-8088}"
DEFAULT_VAPID_SUBJECT="${PAGER_VAPID_SUBJECT:-mailto:admin@racher.local}"

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }
env_value() {
  sudo awk -F= -v key="$1" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "$GATEWAY_ENV" 2>/dev/null || true
}
ensure_env() {
  local key="$1" value="$2"
  if ! sudo grep -q "^${key}=" "$GATEWAY_ENV" 2>/dev/null; then
    echo "$key=$value" | sudo tee -a "$GATEWAY_ENV" >/dev/null
  fi
}

step "1/10 Grundpakker, Docker og NetworkManager"
sudo -v
sudo apt-get update
sudo apt-get install -y \
  ca-certificates curl git python3 sqlite3 alsa-utils docker.io network-manager
sudo systemctl enable --now docker.service NetworkManager.service

if ! sudo docker compose version >/dev/null 2>&1; then
  if sudo apt-get install -y docker-compose-v2 >/dev/null 2>&1; then :
  elif sudo apt-get install -y docker-compose-plugin >/dev/null 2>&1; then :
  elif sudo apt-get install -y docker-compose >/dev/null 2>&1; then :
  fi
fi
if ! sudo docker compose version >/dev/null 2>&1 && ! command -v docker-compose >/dev/null 2>&1; then
  echo "Kunne ikke finde/installere Docker Compose." >&2
  exit 1
fi

step "2/10 Isoleret runtime-repository"
sudo mkdir -p "$(dirname "$RUNTIME_REPO")"
if [[ ! -d "$RUNTIME_REPO/.git" ]]; then
  sudo git clone --branch "$DEPLOY_BRANCH" --single-branch "$ORIGIN_URL" "$RUNTIME_REPO"
else
  sudo git -C "$RUNTIME_REPO" remote set-url origin "$ORIGIN_URL"
  sudo git -C "$RUNTIME_REPO" fetch --prune origin "$DEPLOY_BRANCH"
  sudo git -C "$RUNTIME_REPO" checkout -B pager-runtime FETCH_HEAD
  sudo git -C "$RUNTIME_REPO" reset --hard FETCH_HEAD
fi

SERVICE_DIR="$RUNTIME_REPO/services/pager-gateway"
PDL_DIR="$SERVICE_DIR/pdl"
COMPOSE_FILE="$RUNTIME_REPO/compose/pager-gateway/docker-compose.yml"
for required in \
  "$PDL_DIR/install-pdl.sh" \
  "$PDL_DIR/install-pdl-service.sh" \
  "$PDL_DIR/install-system-agent.sh" \
  "$PDL_DIR/install-backup-service.sh" \
  "$PDL_DIR/install-network-mobility.sh" \
  "$PDL_DIR/pager-compose.sh" \
  "$COMPOSE_FILE"; do
  [[ -f "$required" ]] || { echo "Mangler installationsfil: $required" >&2; exit 1; }
done

step "3/10 Dataområder og lokal konfiguration"
sudo mkdir -p "$ENV_DIR"
if [[ ! -f "$GATEWAY_ENV" ]]; then
  sudo tee "$GATEWAY_ENV" >/dev/null <<EOF
PAGER_DATA_HOST_PATH=$DEFAULT_STATE_ROOT
PAGER_GATEWAY_PORT=$DEFAULT_GATEWAY_PORT
PAGER_COOKIE_SECURE=0
PAGER_VAPID_SUBJECT=$DEFAULT_VAPID_SUBJECT
PAGER_RUNTIME_REPO=$RUNTIME_REPO
PAGER_DEPLOY_BRANCH=$DEPLOY_BRANCH
PAGER_REPO_URL=$ORIGIN_URL
PAGER_PUBLIC_HOSTNAME=
EOF
  sudo chmod 0640 "$GATEWAY_ENV"
else
  ensure_env PAGER_RUNTIME_REPO "$RUNTIME_REPO"
  ensure_env PAGER_DEPLOY_BRANCH "$DEPLOY_BRANCH"
  ensure_env PAGER_REPO_URL "$ORIGIN_URL"
  ensure_env PAGER_PUBLIC_HOSTNAME ""
fi

STATE_ROOT="$(env_value PAGER_DATA_HOST_PATH)"; STATE_ROOT="${STATE_ROOT:-$DEFAULT_STATE_ROOT}"
GATEWAY_PORT="$(env_value PAGER_GATEWAY_PORT)"; GATEWAY_PORT="${GATEWAY_PORT:-$DEFAULT_GATEWAY_PORT}"
COOKIE_SECURE="$(env_value PAGER_COOKIE_SECURE)"; COOKIE_SECURE="${COOKIE_SECURE:-0}"
VAPID_SUBJECT="$(env_value PAGER_VAPID_SUBJECT)"; VAPID_SUBJECT="${VAPID_SUBJECT:-$DEFAULT_VAPID_SUBJECT}"

sudo mkdir -p "$STATE_ROOT" "$STATE_ROOT/pdl" "$STATE_ROOT/update" "$BACKUP_DIR" /opt/racher-pager/integration
sudo chown -R "$(id -un):$(id -gn)" "$STATE_ROOT"
sudo chmod 0750 "$STATE_ROOT"
sudo chmod 0700 "$BACKUP_DIR"
sudo install -m 0755 "$PDL_DIR/pager-compose.sh" /opt/racher-pager/integration/pager-compose.sh

compose() {
  sudo env PAGER_GATEWAY_ENV="$GATEWAY_ENV" /opt/racher-pager/integration/pager-compose.sh "$@"
}

step "4/10 Byg PDL 3.2.0 headless"
bash "$PDL_DIR/install-pdl.sh"

step "5/10 PDL-service"
PAGER_STATE_ROOT="$STATE_ROOT" bash "$PDL_DIR/install-pdl-service.sh"
sudo systemctl start racher-pdl.service >/dev/null 2>&1 || true

step "6/10 Wi-Fi mobility og fallback-portal"
REPO_ROOT="$RUNTIME_REPO" bash "$PDL_DIR/install-network-mobility.sh"

step "7/10 Byg og start Pager Gateway"
compose build --pull pager-gateway
compose up -d --remove-orphans

READY=0
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:$GATEWAY_PORT/healthz" >/dev/null 2>&1; then READY=1; break; fi
  sleep 2
done
if [[ "$READY" != "1" ]]; then
  echo "Gatewayen blev ikke klar inden for 120 sekunder." >&2
  compose ps || true
  exit 1
fi

sudo sqlite3 "$STATE_ROOT/pager.db" <<'SQL'
INSERT INTO settings(key, value) VALUES ('source_mode', 'pdl-file')
ON CONFLICT(key) DO UPDATE SET value=excluded.value;
INSERT INTO settings(key, value) VALUES ('pdl_log_path', '/data/pdl.log')
ON CONFLICT(key) DO UPDATE SET value=excluded.value;
SQL

step "8/10 System-agent, diagnostik og sikre admin-handlinger"
REPO_ROOT="$RUNTIME_REPO" \
PAGER_RUNTIME_REPO="$RUNTIME_REPO" \
PAGER_DATA_HOST_PATH="$STATE_ROOT" PAGER_BACKUP_DIR="$BACKUP_DIR" \
  bash "$PDL_DIR/install-system-agent.sh"

step "9/10 Daglig backup og rollback-reference"
PAGER_STATE_ROOT="$STATE_ROOT" PAGER_BACKUP_DIR="$BACKUP_DIR" \
  bash "$PDL_DIR/install-backup-service.sh"
CURRENT_SHA="$(sudo git -C "$RUNTIME_REPO" rev-parse HEAD)"
printf '%s\n' "$CURRENT_SHA" | sudo tee "$STATE_ROOT/update/current-sha" >/dev/null

step "10/10 Slutkontrol"
sleep 4
GATEWAY_STATE="$(sudo docker inspect --format '{{.State.Status}}' racher-pager-gateway 2>/dev/null || echo unknown)"
PDL_STATE="$(systemctl is-active racher-pdl.service 2>/dev/null || true)"
AGENT_STATE="$(systemctl is-active racher-pager-system-agent.service 2>/dev/null || true)"
FSK_TIMER_STATE="$(systemctl is-active racher-pager-fsk-status.timer 2>/dev/null || true)"
NETWORK_PORTAL_STATE="$(systemctl is-active racher-pager-network-portal.service 2>/dev/null || true)"
BACKUP_TIMER_STATE="$(systemctl is-active racher-pager-backup.timer 2>/dev/null || true)"
FSK_DEVICE=""
shopt -s nullglob
FSK_CANDIDATES=(/dev/serial/by-id/* /dev/ttyUSB* /dev/ttyACM*)
shopt -u nullglob
if (( ${#FSK_CANDIDATES[@]} > 0 )); then FSK_DEVICE="${FSK_CANDIDATES[0]}"; fi
IP_ADDRESS="$(hostname -I 2>/dev/null | awk '{print $1}')"

HOTSPOT_SSID="$(sudo awk -F= '$1=="PAGER_HOTSPOT_SSID"{print substr($0,index($0,"=")+1)}' /etc/racher-pager/network.env)"
HOTSPOT_PASSWORD="$(sudo awk -F= '$1=="PAGER_HOTSPOT_PASSWORD"{print substr($0,index($0,"=")+1)}' /etc/racher-pager/network.env)"
HOTSPOT_IP="$(sudo awk -F= '$1=="PAGER_HOTSPOT_IP"{print substr($0,index($0,"=")+1)}' /etc/racher-pager/network.env)"

cat <<EOF

Racher Pager Gateway er klargjort.

  Gateway container : ${GATEWAY_STATE:-unknown}
  System-agent      : ${AGENT_STATE:-unknown}
  PDL service       : ${PDL_STATE:-unknown}
  FSK status probe  : ${FSK_TIMER_STATE:-unknown}
  FSK-USB           : ${FSK_DEVICE:-afventer hardware}
  Network portal    : ${NETWORK_PORTAL_STATE:-unknown}
  Backup timer      : ${BACKUP_TIMER_STATE:-unknown}
  Runtime commit    : ${CURRENT_SHA:0:12}
  Data              : $STATE_ROOT
  Backups           : $BACKUP_DIR

Lokal web:
  http://127.0.0.1:$GATEWAY_PORT
EOF
[[ -n "$IP_ADDRESS" ]] && echo "  http://$IP_ADDRESS:$GATEWAY_PORT"
cat <<EOF

Fallback hvis Pi'en ikke kan komme online:
  Wi-Fi             : $HOTSPOT_SSID
  Password / PIN    : $HOTSPOT_PASSWORD
  Setup portal      : http://$HOTSPOT_IP/

Gem Password/PIN et sikkert sted.
Det er OK hvis FSK-USB/PDL endnu ikke er klar. Scanner-testen udføres senere.
Cloudflare Tunnel kan installeres, når tunnel-token og hostname er klar, med:
  bash $PDL_DIR/install-cloudflared.sh <TUNNEL_TOKEN> <pager.ditdomæne.dk>
EOF

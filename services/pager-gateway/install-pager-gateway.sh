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

REPO_ROOT="${REPO_ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || true)}"
if [[ -z "$REPO_ROOT" ]]; then
  echo "Kør scriptet fra et checkout af Racher-Homelab." >&2
  exit 1
fi

SERVICE_DIR="$REPO_ROOT/services/pager-gateway"
PDL_DIR="$SERVICE_DIR/pdl"
COMPOSE_FILE="$REPO_ROOT/compose/pager-gateway/docker-compose.yml"
DEFAULT_STATE_ROOT="${PAGER_STATE_ROOT:-/var/lib/racher-pager}"
BACKUP_DIR="${PAGER_BACKUP_DIR:-/var/backups/racher-pager}"
ENV_DIR="/etc/racher-pager"
GATEWAY_ENV="$ENV_DIR/gateway.env"
DEFAULT_GATEWAY_PORT="${PAGER_GATEWAY_PORT:-8088}"
DEFAULT_VAPID_SUBJECT="${PAGER_VAPID_SUBJECT:-mailto:admin@racher.local}"

for required in \
  "$PDL_DIR/install-pdl.sh" \
  "$PDL_DIR/install-pdl-service.sh" \
  "$PDL_DIR/install-system-agent.sh" \
  "$PDL_DIR/install-backup-service.sh" \
  "$COMPOSE_FILE"; do
  if [[ ! -f "$required" ]]; then
    echo "Mangler installationsfil: $required" >&2
    exit 1
  fi
done

step() {
  printf '\n\033[1;36m==> %s\033[0m\n' "$1"
}

env_value() {
  sudo awk -F= -v key="$1" '
    $1 == key {
      sub(/^[^=]*=/, "")
      print
      exit
    }
  ' "$GATEWAY_ENV" 2>/dev/null || true
}

step "1/9 Grundpakker og Docker"
sudo -v
sudo apt-get update
sudo apt-get install -y \
  ca-certificates curl git python3 sqlite3 alsa-utils docker.io
sudo systemctl enable --now docker.service

COMPOSE_KIND=""
if sudo docker compose version >/dev/null 2>&1; then
  COMPOSE_KIND="plugin"
else
  # Debian/Raspberry Pi OS-versioner har brugt forskellige pakkenavne over tid.
  if sudo apt-get install -y docker-compose-v2 >/dev/null 2>&1; then
    :
  elif sudo apt-get install -y docker-compose-plugin >/dev/null 2>&1; then
    :
  elif sudo apt-get install -y docker-compose >/dev/null 2>&1; then
    :
  fi

  if sudo docker compose version >/dev/null 2>&1; then
    COMPOSE_KIND="plugin"
  elif sudo docker-compose version >/dev/null 2>&1; then
    COMPOSE_KIND="legacy"
  else
    echo "Kunne ikke finde/installere Docker Compose." >&2
    exit 1
  fi
fi

step "2/9 Dataområder og lokal konfiguration"
sudo mkdir -p "$ENV_DIR"
if [[ ! -f "$GATEWAY_ENV" ]]; then
  sudo tee "$GATEWAY_ENV" >/dev/null <<EOF
# Lokale bootstrap-værdier. HTTPS sættes op i et senere trin.
PAGER_DATA_HOST_PATH=$DEFAULT_STATE_ROOT
PAGER_GATEWAY_PORT=$DEFAULT_GATEWAY_PORT
PAGER_COOKIE_SECURE=0
PAGER_VAPID_SUBJECT=$DEFAULT_VAPID_SUBJECT
EOF
  sudo chmod 0640 "$GATEWAY_ENV"
fi

# Eksisterende konfiguration vinder. Det er vigtigt, hvis bootstrap køres igen
# efter HTTPS er sat op, så secure cookies/port ikke bliver nulstillet.
STATE_ROOT="$(env_value PAGER_DATA_HOST_PATH)"
GATEWAY_PORT="$(env_value PAGER_GATEWAY_PORT)"
COOKIE_SECURE="$(env_value PAGER_COOKIE_SECURE)"
VAPID_SUBJECT="$(env_value PAGER_VAPID_SUBJECT)"
STATE_ROOT="${STATE_ROOT:-$DEFAULT_STATE_ROOT}"
GATEWAY_PORT="${GATEWAY_PORT:-$DEFAULT_GATEWAY_PORT}"
COOKIE_SECURE="${COOKIE_SECURE:-0}"
VAPID_SUBJECT="${VAPID_SUBJECT:-$DEFAULT_VAPID_SUBJECT}"

sudo mkdir -p "$STATE_ROOT" "$STATE_ROOT/pdl" "$BACKUP_DIR"
sudo chown -R "$(id -un):$(id -gn)" "$STATE_ROOT"
sudo chmod 0750 "$STATE_ROOT"
sudo chmod 0700 "$BACKUP_DIR"

compose() {
  if [[ "$COMPOSE_KIND" == "plugin" ]]; then
    sudo env \
      PAGER_DATA_HOST_PATH="$STATE_ROOT" \
      PAGER_GATEWAY_PORT="$GATEWAY_PORT" \
      PAGER_COOKIE_SECURE="$COOKIE_SECURE" \
      PAGER_VAPID_SUBJECT="$VAPID_SUBJECT" \
      docker compose -f "$COMPOSE_FILE" "$@"
  else
    sudo env \
      PAGER_DATA_HOST_PATH="$STATE_ROOT" \
      PAGER_GATEWAY_PORT="$GATEWAY_PORT" \
      PAGER_COOKIE_SECURE="$COOKIE_SECURE" \
      PAGER_VAPID_SUBJECT="$VAPID_SUBJECT" \
      docker-compose -f "$COMPOSE_FILE" "$@"
  fi
}

step "3/9 Byg PDL 3.2.0 headless"
bash "$PDL_DIR/install-pdl.sh"

step "4/9 Installer og aktivér PDL-service"
PAGER_STATE_ROOT="$STATE_ROOT" bash "$PDL_DIR/install-pdl-service.sh"
# Uden capture-hardware kan PDL stå og genstarte. Det er forventet hjemme før scanner-testen.
sudo systemctl start racher-pdl.service >/dev/null 2>&1 || true

step "5/9 Byg og start Pager Gateway"
compose build --pull
compose up -d --remove-orphans

step "6/9 Vent på gateway-healthcheck"
READY=0
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:$GATEWAY_PORT/healthz" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 2
done
if [[ "$READY" != "1" ]]; then
  echo "Gatewayen blev ikke klar inden for 120 sekunder." >&2
  compose ps || true
  exit 1
fi

# På Pi er den rigtige inputkilde PDL. Simulatoren forbliver tilgængelig for admin-tests.
sudo sqlite3 "$STATE_ROOT/pager.db" <<'SQL'
INSERT INTO settings(key, value) VALUES ('source_mode', 'pdl-file')
ON CONFLICT(key) DO UPDATE SET value=excluded.value;
INSERT INTO settings(key, value) VALUES ('pdl_log_path', '/data/pdl.log')
ON CONFLICT(key) DO UPDATE SET value=excluded.value;
SQL

step "7/9 Installer sikker system-agent og live status"
PAGER_DATA_HOST_PATH="$STATE_ROOT" PAGER_BACKUP_DIR="$BACKUP_DIR" \
  bash "$PDL_DIR/install-system-agent.sh"

step "8/9 Installer daglig backup og lav første backup"
PAGER_STATE_ROOT="$STATE_ROOT" PAGER_BACKUP_DIR="$BACKUP_DIR" \
  bash "$PDL_DIR/install-backup-service.sh"

step "9/9 Slutkontrol"
sleep 3
GATEWAY_STATE="$(sudo docker inspect --format '{{.State.Status}}' racher-pager-gateway 2>/dev/null || echo unknown)"
PDL_STATE="$(systemctl is-active racher-pdl.service 2>/dev/null || true)"
AGENT_STATE="$(systemctl is-active racher-pager-system-agent.service 2>/dev/null || true)"
BACKUP_TIMER_STATE="$(systemctl is-active racher-pager-backup.timer 2>/dev/null || true)"
AUDIO_COUNT="$(arecord -l 2>/dev/null | grep -Eic '^card[[:space:]]+[0-9]+:' || true)"
IP_ADDRESS="$(hostname -I 2>/dev/null | awk '{print $1}')"

cat <<EOF

Racher Pager Gateway er klargjort.

  Gateway container : ${GATEWAY_STATE:-unknown}
  System-agent      : ${AGENT_STATE:-unknown}
  PDL service       : ${PDL_STATE:-unknown}
  ALSA capture      : $AUDIO_COUNT enhed(er)
  Backup timer      : ${BACKUP_TIMER_STATE:-unknown}
  Data              : $STATE_ROOT
  Backups           : $BACKUP_DIR

Lokal health/web-port:
  http://127.0.0.1:$GATEWAY_PORT
EOF

if [[ -n "$IP_ADDRESS" ]]; then
  echo "  http://$IP_ADDRESS:$GATEWAY_PORT"
fi

cat <<'EOF'

Det er OK hvis USB lydinput/PDL ikke er klar endnu. Scanner-testen kan udføres senere.
Næste online-trin bliver HTTPS/domæne, hvorefter PWA Web Push kan aktiveres sikkert.
EOF

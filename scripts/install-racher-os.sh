#!/usr/bin/env bash
set -Eeuo pipefail

log() { printf '[installer] %s\n' "$*"; }
fail() { printf '[installer] FEJL: %s\n' "$*" >&2; exit 1; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${RACHER_ENV_FILE:-$REPO_ROOT/.env}"
HEALTH_URL="${RACHER_HEALTH_URL:-http://127.0.0.1:81}"
HEALTH_TIMEOUT="${RACHER_HEALTH_TIMEOUT:-120}"
STACKS=(compose/data/compose.yml compose/core/compose.yml)
REQUIRED_SECRETS=(NPM_DB_PASSWORD NPM_DB_ROOT_PASSWORD POSTGRES_PASSWORD)
STARTED=()

rollback() {
  local exit_code=$?
  if [[ "$exit_code" -eq 0 ]]; then return; fi
  printf '[installer] Installation fejlede; stopper stacks startet i denne kørsel.\n' >&2
  for (( index=${#STARTED[@]}-1; index>=0; index-- )); do
    docker compose --env-file "$ENV_FILE" -f "$REPO_ROOT/${STARTED[$index]}" down --remove-orphans || true
  done
  exit "$exit_code"
}
trap rollback EXIT

read_env_value() {
  local key="$1"
  sed -n "s/^${key}=//p" "$ENV_FILE" | tail -n 1
}

[[ "${EUID}" -ne 0 ]] || fail "Kør som normal bruger, ikke root."
[[ "$HEALTH_TIMEOUT" =~ ^[0-9]+$ ]] || fail "RACHER_HEALTH_TIMEOUT skal være et heltal."
(( HEALTH_TIMEOUT >= 30 && HEALTH_TIMEOUT <= 600 )) || fail "Health timeout skal være 30-600 sekunder."
command -v curl >/dev/null 2>&1 || fail "curl mangler."
command -v docker >/dev/null 2>&1 || fail "Docker mangler. Kør scripts/bootstrap.sh først."
docker compose version >/dev/null 2>&1 || fail "Docker Compose-plugin mangler."
docker info >/dev/null 2>&1 || fail "Docker kan ikke tilgås. Log ud og ind efter bootstrap."

if [[ ! -f "$ENV_FILE" ]]; then
  cp "$REPO_ROOT/.env.example" "$ENV_FILE"
  chmod 0600 "$ENV_FILE"
  fail ".env er oprettet. Udfyld de obligatoriske værdier og kør igen."
fi
chmod 0600 "$ENV_FILE"

for key in "${REQUIRED_SECRETS[@]}"; do
  value="$(read_env_value "$key")"
  [[ -n "$value" && "$value" != CHANGE_ME* ]] || fail "$key mangler eller bruger en placeholder."
  (( ${#value} >= 16 )) || fail "$key skal være mindst 16 tegn."
done

for stack in "${STACKS[@]}"; do
  [[ -f "$REPO_ROOT/$stack" ]] || fail "Compose-fil mangler: $stack"
  docker compose --env-file "$ENV_FILE" -f "$REPO_ROOT/$stack" config --quiet
done

log "Henter container-images"
for stack in "${STACKS[@]}"; do
  docker compose --env-file "$ENV_FILE" -f "$REPO_ROOT/$stack" pull
done

log "Starter stacks"
for stack in "${STACKS[@]}"; do
  docker compose --env-file "$ENV_FILE" -f "$REPO_ROOT/$stack" up -d --remove-orphans
  STARTED+=("$stack")
done

log "Venter på core health endpoint: $HEALTH_URL"
deadline=$((SECONDS + HEALTH_TIMEOUT))
until curl --fail --silent --show-error --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; do
  (( SECONDS < deadline )) || fail "Healthcheck fejlede efter ${HEALTH_TIMEOUT}s: $HEALTH_URL"
  sleep 3
done

trap - EXIT
log "Racher OS core er startet og healthcheck er grønt"
docker compose --env-file "$ENV_FILE" -f "$REPO_ROOT/compose/data/compose.yml" ps
docker compose --env-file "$ENV_FILE" -f "$REPO_ROOT/compose/core/compose.yml" ps

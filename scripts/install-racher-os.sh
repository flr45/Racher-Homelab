#!/usr/bin/env bash
set -Eeuo pipefail

log() { printf '[installer] %s\n' "$*"; }
fail() { printf '[installer] FEJL: %s\n' "$*" >&2; exit 1; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${RACHER_ENV_FILE:-$REPO_ROOT/.env}"
HEALTH_URL="${RACHER_HEALTH_URL:-http://127.0.0.1:5000/api/status}"
HEALTH_TIMEOUT="${RACHER_HEALTH_TIMEOUT:-120}"
STACKS=(compose/data/compose.yml compose/core/compose.yml)
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

[[ "${EUID}" -ne 0 ]] || fail "Kør som normal bruger, ikke root."
command -v docker >/dev/null 2>&1 || fail "Docker mangler. Kør scripts/bootstrap.sh først."
docker compose version >/dev/null 2>&1 || fail "Docker Compose-plugin mangler."
docker info >/dev/null 2>&1 || fail "Docker kan ikke tilgås. Log ud og ind efter bootstrap."

if [[ ! -f "$ENV_FILE" ]]; then
  cp "$REPO_ROOT/.env.example" "$ENV_FILE"
  chmod 0600 "$ENV_FILE"
  fail ".env er oprettet. Erstat alle CHANGE_ME-værdier og kør igen."
fi
chmod 0600 "$ENV_FILE"

if grep -Eq '(^|=)(CHANGE_ME|CHANGE_ME_LONG_RANDOM_SECRET)$' "$ENV_FILE"; then
  fail ".env indeholder stadig obligatoriske CHANGE_ME-værdier."
fi

for stack in "${STACKS[@]}"; do
  [[ -f "$REPO_ROOT/$stack" ]] || fail "Compose-fil mangler: $stack"
  docker compose --env-file "$ENV_FILE" -f "$REPO_ROOT/$stack" config --quiet
 done

log "Henter container-images"
for stack in "${STACKS[@]}"; do
  docker compose --env-file "$ENV_FILE" -f "$REPO_ROOT/$stack" pull
 done

log "Starter datastack"
for stack in "${STACKS[@]}"; do
  docker compose --env-file "$ENV_FILE" -f "$REPO_ROOT/$stack" up -d --remove-orphans
  STARTED+=("$stack")
 done

log "Venter på Control Center health endpoint"
deadline=$((SECONDS + HEALTH_TIMEOUT))
until curl --fail --silent --show-error --max-time 5 "$HEALTH_URL" >/dev/null 2>&1; do
  (( SECONDS < deadline )) || fail "Healthcheck fejlede efter ${HEALTH_TIMEOUT}s: $HEALTH_URL"
  sleep 3
 done

trap - EXIT
log "Racher OS er startet og healthcheck er grønt"
docker compose --env-file "$ENV_FILE" -f "$REPO_ROOT/compose/core/compose.yml" ps

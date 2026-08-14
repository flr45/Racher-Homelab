#!/usr/bin/env bash
set -euo pipefail

ENV_FILE="${PAGER_GATEWAY_ENV:-/etc/racher-pager/gateway.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

RUNTIME_REPO="${PAGER_RUNTIME_REPO:-/opt/racher-pager/runtime-repo}"
COMPOSE_FILE="$RUNTIME_REPO/compose/pager-gateway/docker-compose.yml"

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "Mangler compose-fil: $COMPOSE_FILE" >&2
  exit 1
fi

export PAGER_DATA_HOST_PATH="${PAGER_DATA_HOST_PATH:-/var/lib/racher-pager}"
export PAGER_GATEWAY_PORT="${PAGER_GATEWAY_PORT:-8088}"
export PAGER_COOKIE_SECURE="${PAGER_COOKIE_SECURE:-0}"
export PAGER_VAPID_SUBJECT="${PAGER_VAPID_SUBJECT:-mailto:admin@racher.local}"

if docker compose version >/dev/null 2>&1; then
  exec docker compose -f "$COMPOSE_FILE" "$@"
elif command -v docker-compose >/dev/null 2>&1; then
  exec docker-compose -f "$COMPOSE_FILE" "$@"
else
  echo "Docker Compose mangler." >&2
  exit 1
fi

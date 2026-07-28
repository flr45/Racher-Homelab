#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${HOMELAB_ROOT:-$HOME/homelab/Racher-Homelab}"
ENV_FILE="${ENV_FILE:-$ROOT/.env}"
cd "$ROOT"

[[ -f "$ENV_FILE" ]] || { echo "Miljøfilen mangler: $ENV_FILE" >&2; exit 1; }

git pull --ff-only

for stack in compose/core compose/data compose/minutregnskab compose/control-center; do
  echo "Validerer $stack"
  docker compose --env-file "$ENV_FILE" -f "$stack/compose.yml" config --quiet
  echo "Opdaterer $stack"
  docker compose --env-file "$ENV_FILE" -f "$stack/compose.yml" pull
  docker compose --env-file "$ENV_FILE" -f "$stack/compose.yml" up -d --remove-orphans
  docker compose --env-file "$ENV_FILE" -f "$stack/compose.yml" ps
 done

docker image prune -f

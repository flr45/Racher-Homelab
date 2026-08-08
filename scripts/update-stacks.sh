#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="${HOMELAB_ROOT:-$REPO_ROOT}"
ENV_FILE="${ENV_FILE:-$ROOT/.env}"
cd "$ROOT"

[[ -f "$ENV_FILE" ]] || { echo "Miljøfilen mangler: $ENV_FILE" >&2; exit 1; }

git pull --ff-only

for stack in compose/core compose/data compose/minutregnskab compose/indsatsbrief compose/control-center compose/vagtbytte; do
  compose_file="$stack/compose.yml"
  if [[ ! -f "$compose_file" ]]; then
    echo "Springer over $stack: $compose_file findes ikke"
    continue
  fi

  echo "Validerer $stack"
  docker compose --env-file "$ENV_FILE" -f "$compose_file" config --quiet
  echo "Opdaterer $stack"
  docker compose --env-file "$ENV_FILE" -f "$compose_file" pull
  docker compose --env-file "$ENV_FILE" -f "$compose_file" up -d --remove-orphans
  docker compose --env-file "$ENV_FILE" -f "$compose_file" ps
done

docker image prune -f

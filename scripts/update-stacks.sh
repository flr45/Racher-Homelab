#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${HOMELAB_ROOT:-$HOME/homelab/Racher-Homelab}"
cd "$ROOT"

git pull --ff-only

for stack in compose/core compose/data compose/minutregnskab; do
  echo "Opdaterer $stack"
  docker compose --env-file .env -f "$stack/compose.yml" pull
  docker compose --env-file .env -f "$stack/compose.yml" up -d --remove-orphans
done

docker image prune -f

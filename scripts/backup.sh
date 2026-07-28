#!/usr/bin/env bash
set -Eeuo pipefail

BACKUP_ROOT="${BACKUP_ROOT:-$HOME/homelab/backups}"
STAMP="$(date +%Y-%m-%d_%H-%M-%S)"
DEST="$BACKUP_ROOT/$STAMP"
mkdir -p "$DEST"

backup_volume() {
  local volume="$1"
  local filename="$2"
  docker run --rm \
    -v "${volume}:/source:ro" \
    -v "$DEST:/backup" \
    alpine:3.20 \
    tar -czf "/backup/${filename}.tar.gz" -C /source .
}

backup_volume racher-homelab-core_npm_db_data npm-db
backup_volume racher-homelab-core_npm_data npm-data
backup_volume racher-homelab-core_npm_letsencrypt npm-letsencrypt
backup_volume racher-homelab-core_portainer_data portainer
backup_volume racher-homelab-core_uptime_kuma_data uptime-kuma
backup_volume racher-homelab-data_postgres_data postgres
backup_volume racher-homelab-data_redis_data redis

find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -mtime +14 -exec rm -rf {} +

echo "Backup gemt i $DEST"

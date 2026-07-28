#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${HOMELAB_ROOT:-$HOME/homelab/Racher-Homelab}"
ENV_FILE="${ENV_FILE:-$ROOT/.env}"
BACKUP_ROOT="${BACKUP_ROOT:-$HOME/homelab/backups}"
BACKUP_MIRROR_DIR="${BACKUP_MIRROR_DIR:-}"
CONTROL_CENTER_VOLUME="${CONTROL_CENTER_VOLUME:-racher-control-center_control-center-data}"
STAMP="$(date +%Y-%m-%d_%H-%M-%S)"
DEST="$BACKUP_ROOT/$STAMP"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

fail() {
  log "FEJL: $*"
  exit 1
}

command -v docker >/dev/null 2>&1 || fail "Docker blev ikke fundet."
[[ -f "$ENV_FILE" ]] || fail "Miljøfilen mangler: $ENV_FILE"

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

: "${POSTGRES_USER:?POSTGRES_USER mangler i .env}"
: "${POSTGRES_DB:?POSTGRES_DB mangler i .env}"
: "${NPM_DB_USER:?NPM_DB_USER mangler i .env}"
: "${NPM_DB_PASSWORD:?NPM_DB_PASSWORD mangler i .env}"
: "${NPM_DB_NAME:?NPM_DB_NAME mangler i .env}"

mkdir -p "$DEST"
chmod 700 "$DEST"
trap 'log "Backup mislykkedes. Den ufuldstændige mappe bevares i $DEST"' ERR

container_running() {
  [[ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null || true)" == "true" ]]
}

backup_volume() {
  local volume="$1"
  local filename="$2"
  local required="${3:-false}"

  if ! docker volume inspect "$volume" >/dev/null 2>&1; then
    if [[ "$required" == "true" ]]; then
      fail "Påkrævet volume mangler: $volume"
    fi
    log "Springer over manglende volume: $volume"
    return 0
  fi

  log "Sikkerhedskopierer volume $volume"
  docker run --rm \
    -v "${volume}:/source:ro" \
    -v "$DEST:/backup" \
    alpine:3.20 \
    tar -czf "/backup/${filename}.tar.gz" -C /source .
}

log "Starter backup til $DEST"

if container_running postgres; then
  log "Opretter konsistent PostgreSQL-dump"
  docker exec postgres pg_dump \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    --format=custom \
    --no-owner \
    --no-privileges > "$DEST/postgres.dump"
else
  fail "PostgreSQL-containeren kører ikke."
fi

if container_running npm-db; then
  log "Opretter konsistent MariaDB-dump til Nginx Proxy Manager"
  docker exec \
    -e MYSQL_PWD="$NPM_DB_PASSWORD" \
    npm-db mariadb-dump \
    --user="$NPM_DB_USER" \
    --single-transaction \
    --quick \
    --skip-lock-tables \
    "$NPM_DB_NAME" | gzip -9 > "$DEST/npm-database.sql.gz"
else
  fail "Nginx Proxy Manager-databasen kører ikke."
fi

if container_running redis; then
  log "Beder Redis skrive data til disk"
  docker exec redis redis-cli SAVE >/dev/null
fi

backup_volume racher-homelab-core_npm_data npm-data
backup_volume racher-homelab-core_npm_letsencrypt npm-letsencrypt
backup_volume racher-homelab-core_portainer_data portainer
backup_volume racher-homelab-core_uptime_kuma_data uptime-kuma
backup_volume racher-homelab-data_redis_data redis
backup_volume "$CONTROL_CENTER_VOLUME" control-center-data true

cp "$ENV_FILE" "$DEST/env.backup"
chmod 600 "$DEST/env.backup"

cat > "$DEST/MANIFEST.json" <<EOF
{
  "format_version": 1,
  "created_at": "$(date --iso-8601=seconds)",
  "host": "$(hostname)",
  "control_center_volume": "$CONTROL_CENTER_VOLUME",
  "postgres_database": "$POSTGRES_DB",
  "npm_database": "$NPM_DB_NAME"
}
EOF

(
  cd "$DEST"
  find . -maxdepth 1 -type f ! -name SHA256SUMS -printf '%f\n' \
    | sort \
    | xargs -r sha256sum > SHA256SUMS
  sha256sum -c SHA256SUMS
)

ln -sfn "$DEST" "$BACKUP_ROOT/latest"

if [[ -n "$BACKUP_MIRROR_DIR" ]]; then
  mkdir -p "$BACKUP_MIRROR_DIR"
  log "Kopierer backup til ekstern placering: $BACKUP_MIRROR_DIR"
  cp -a "$DEST" "$BACKUP_MIRROR_DIR/"
fi

trap - ERR
log "Backup færdig: $DEST"
log "Kontrol: cd '$DEST' && sha256sum -c SHA256SUMS"
log "Oprydning af gamle backups udføres bevidst separat efter validering."

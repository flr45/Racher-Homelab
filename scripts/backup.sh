#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$REPO_ROOT/.env}"
STAMP="$(date +%Y-%m-%d_%H-%M-%S)"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

fail() {
  log "FEJL: $*"
  exit 1
}

read_env_value() {
  local key="$1"
  local value

  value="$(sed -n "s/^${key}=//p" "$ENV_FILE" | tail -n 1 | tr -d '\r')"
  if [[ "${#value}" -ge 2 ]]; then
    case "$value" in
      \"*\") value="${value:1:${#value}-2}" ;;
      \'*\') value="${value:1:${#value}-2}" ;;
    esac
  fi
  printf '%s' "$value"
}

command -v docker >/dev/null 2>&1 || fail "Docker blev ikke fundet."
[[ -f "$ENV_FILE" ]] || fail "Miljøfilen mangler: $ENV_FILE"

BACKUP_ROOT="${BACKUP_ROOT:-$(read_env_value BACKUP_ROOT)}"
BACKUP_ROOT="${BACKUP_ROOT:-$HOME/homelab/backups}"
BACKUP_MIRROR_DIR="${BACKUP_MIRROR_DIR:-$(read_env_value BACKUP_MIRROR_DIR)}"
CONTROL_CENTER_CONTAINER="${CONTROL_CENTER_CONTAINER:-$(read_env_value CONTROL_CENTER_CONTAINER)}"
CONTROL_CENTER_CONTAINER="${CONTROL_CENTER_CONTAINER:-control-center}"
CONTROL_CENTER_DATA_DIR="${CONTROL_CENTER_DATA_DIR:-$(read_env_value CONTROL_CENTER_DATA_DIR)}"
CONTROL_CENTER_DATA_DIR="${CONTROL_CENTER_DATA_DIR:-$HOME/homelab/data}"
CONTROL_CENTER_GID="${CONTROL_CENTER_GID:-$(read_env_value CONTROL_CENTER_GID)}"
CONTROL_CENTER_GID="${CONTROL_CENTER_GID:-1001}"
POSTGRES_USER="${POSTGRES_USER:-$(read_env_value POSTGRES_USER)}"
POSTGRES_DB="${POSTGRES_DB:-$(read_env_value POSTGRES_DB)}"
NPM_DB_USER="${NPM_DB_USER:-$(read_env_value NPM_DB_USER)}"
NPM_DB_PASSWORD="${NPM_DB_PASSWORD:-$(read_env_value NPM_DB_PASSWORD)}"
NPM_DB_NAME="${NPM_DB_NAME:-$(read_env_value NPM_DB_NAME)}"
DEST="$BACKUP_ROOT/$STAMP"
CONTROL_CENTER_STAGE=""

: "${POSTGRES_USER:?POSTGRES_USER mangler i .env}"
: "${POSTGRES_DB:?POSTGRES_DB mangler i .env}"
: "${NPM_DB_USER:?NPM_DB_USER mangler i .env}"
: "${NPM_DB_PASSWORD:?NPM_DB_PASSWORD mangler i .env}"
: "${NPM_DB_NAME:?NPM_DB_NAME mangler i .env}"
[[ "$CONTROL_CENTER_GID" =~ ^[0-9]+$ ]] || fail "CONTROL_CENTER_GID skal være et numerisk gruppe-ID."

[[ -d "$CONTROL_CENTER_DATA_DIR" ]] || fail "Control Center-datamappen mangler: $CONTROL_CENTER_DATA_DIR"
mkdir -p "$DEST"
chmod 700 "$DEST"

cleanup() {
  if [[ -n "$CONTROL_CENTER_STAGE" && -d "$CONTROL_CENTER_STAGE" ]]; then
    rm -rf "$CONTROL_CENTER_STAGE"
  fi
}

on_error() {
  local exit_code=$?
  cleanup
  log "Backup mislykkedes. Den ufuldstændige mappe bevares i $DEST"
  exit "$exit_code"
}

trap on_error ERR

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

normalize_backup_permissions() {
  local host_uid
  host_uid="$(id -u)"

  log "Sikrer læseadgang til Control Center"
  docker run --rm \
    -v "$DEST:/backup" \
    alpine:3.20 \
    sh -eu -c "chown -R ${host_uid}:${CONTROL_CENTER_GID} /backup; find /backup -type d -exec chmod 0750 {} +; find /backup -type f -exec chmod 0640 {} +"
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

if container_running "$CONTROL_CENTER_CONTAINER"; then
  CONTROL_CENTER_STAGE="$(mktemp -d)"

  log "Kopierer øvrige Control Center-data til staging"
  tar \
    --exclude='./racher-os.db' \
    --exclude='./racher-os.db-*' \
    -cf - \
    -C "$CONTROL_CENTER_DATA_DIR" . | tar -xf - -C "$CONTROL_CENTER_STAGE"

  log "Opretter konsistent SQLite-backup af Control Center"
  docker exec "$CONTROL_CENTER_CONTAINER" rm -f /tmp/racher-os-backup.db
  docker exec "$CONTROL_CENTER_CONTAINER" python -c 'import sqlite3; source = sqlite3.connect("/data/racher-os.db"); target = sqlite3.connect("/tmp/racher-os-backup.db"); source.backup(target); target.close(); source.close()'
  docker cp "$CONTROL_CENTER_CONTAINER:/tmp/racher-os-backup.db" "$CONTROL_CENTER_STAGE/racher-os.db"
  docker exec "$CONTROL_CENTER_CONTAINER" rm -f /tmp/racher-os-backup.db

  log "Pakker Control Center-data"
  tar -czf "$DEST/control-center-data.tar.gz" -C "$CONTROL_CENTER_STAGE" .
  cleanup
  CONTROL_CENTER_STAGE=""
else
  fail "Control Center-containeren kører ikke."
fi

backup_volume racher-homelab-core_npm_data npm-data
backup_volume racher-homelab-core_npm_letsencrypt npm-letsencrypt
backup_volume racher-homelab-core_portainer_data portainer
backup_volume racher-homelab-core_uptime_kuma_data uptime-kuma
backup_volume racher-homelab-data_redis_data redis
backup_volume vagtbytte_vagtbytte_backups vagtbytte-backups
backup_volume vagtbytte_vagtbytte_operativ_portal vagtbytte-operativ-portal

cp "$ENV_FILE" "$DEST/env.backup"
chmod 600 "$DEST/env.backup"

cat > "$DEST/MANIFEST.json" <<EOF
{
  "format_version": 2,
  "created_at": "$(date --iso-8601=seconds)",
  "host": "$(hostname)",
  "control_center_container": "$CONTROL_CENTER_CONTAINER",
  "control_center_data_dir": "$CONTROL_CENTER_DATA_DIR",
  "postgres_database": "$POSTGRES_DB",
  "npm_database": "$NPM_DB_NAME"
}
EOF

(
  cd "$DEST"
  checksum_files="$(mktemp)"
  trap 'rm -f "$checksum_files"' EXIT
  find . -maxdepth 1 -type f ! -name SHA256SUMS -printf '%f\n' | sort > "$checksum_files"
  xargs -r sha256sum < "$checksum_files" > SHA256SUMS
  sha256sum -c SHA256SUMS
)

normalize_backup_permissions
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
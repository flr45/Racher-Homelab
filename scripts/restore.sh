#!/usr/bin/env bash
set -Eeuo pipefail

BACKUP_ROOT="${BACKUP_ROOT:-$HOME/homelab/backups}"
BACKUP_NAME="${1:-}"
MODE="${2:---dry-run}"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

fail() {
  log "FEJL: $*"
  exit 1
}

usage() {
  cat <<'EOF'
Brug:
  ./scripts/restore.sh <backup-navn> [--dry-run|--stage-control-center]

--dry-run                Validerer manifest, checksums og backupfiler uden ændringer.
--stage-control-center   Udpakker Control Center-data i en ny isoleret Docker-volume.

Scriptet overskriver aldrig den aktive volume. Efter staging skal data inspiceres,
og et eventuelt cutover skal udføres separat og dokumenteret.
EOF
}

[[ -n "$BACKUP_NAME" ]] || { usage; exit 2; }
[[ "$BACKUP_NAME" != */* && "$BACKUP_NAME" != .* ]] || fail "Ugyldigt backup-navn."

BACKUP_DIR="$BACKUP_ROOT/$BACKUP_NAME"
[[ -d "$BACKUP_DIR" ]] || fail "Backup blev ikke fundet: $BACKUP_DIR"
[[ -f "$BACKUP_DIR/MANIFEST.json" ]] || fail "MANIFEST.json mangler."
[[ -f "$BACKUP_DIR/SHA256SUMS" ]] || fail "SHA256SUMS mangler."

log "Validerer checksums"
(
  cd "$BACKUP_DIR"
  sha256sum -c SHA256SUMS
)

[[ -s "$BACKUP_DIR/postgres.dump" ]] || fail "postgres.dump mangler eller er tom."
[[ -s "$BACKUP_DIR/npm-database.sql.gz" ]] || fail "npm-database.sql.gz mangler eller er tom."
[[ -s "$BACKUP_DIR/control-center-data.tar.gz" ]] || fail "control-center-data.tar.gz mangler eller er tom."

gzip -t "$BACKUP_DIR/npm-database.sql.gz"
tar -tzf "$BACKUP_DIR/control-center-data.tar.gz" >/dev/null

if command -v pg_restore >/dev/null 2>&1; then
  pg_restore --list "$BACKUP_DIR/postgres.dump" >/dev/null
else
  log "Bemærk: pg_restore findes ikke lokalt; PostgreSQL-dumpens indhold er ikke listet."
fi

log "Backupen er strukturelt gyldig: $BACKUP_NAME"

case "$MODE" in
  --dry-run)
    log "Dry-run færdig. Ingen data er ændret."
    ;;
  --stage-control-center)
    command -v docker >/dev/null 2>&1 || fail "Docker blev ikke fundet."
    STAGING_VOLUME="racher-control-center-restore-${BACKUP_NAME//[^a-zA-Z0-9_.-]/-}"
    if docker volume inspect "$STAGING_VOLUME" >/dev/null 2>&1; then
      fail "Staging-volume findes allerede: $STAGING_VOLUME"
    fi
    docker volume create "$STAGING_VOLUME" >/dev/null
    docker run --rm \
      -v "$STAGING_VOLUME:/restore" \
      -v "$BACKUP_DIR:/backup:ro" \
      alpine:3.20 \
      tar -xzf /backup/control-center-data.tar.gz -C /restore
    log "Control Center-data er staged i volume: $STAGING_VOLUME"
    log "Den aktive volume er ikke ændret. Inspicér staging-volumen før cutover."
    ;;
  *)
    usage
    fail "Ukendt tilstand: $MODE"
    ;;
esac

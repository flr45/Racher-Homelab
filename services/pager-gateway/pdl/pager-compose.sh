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

# The state directory is owned by the normal appliance user. Run Gunicorn with
# that same numeric uid/gid instead of root so a compromised web process cannot
# gain host-level privileges through container defaults. Existing installations
# need no manual env migration: derive the ids from the mounted state directory.
if [[ -z "${PAGER_RUNTIME_UID:-}" || -z "${PAGER_RUNTIME_GID:-}" ]]; then
  if [[ -d "$PAGER_DATA_HOST_PATH" ]]; then
    PAGER_RUNTIME_UID="${PAGER_RUNTIME_UID:-$(stat -c '%u' "$PAGER_DATA_HOST_PATH")}"
    PAGER_RUNTIME_GID="${PAGER_RUNTIME_GID:-$(stat -c '%g' "$PAGER_DATA_HOST_PATH")}"
  fi
fi
export PAGER_RUNTIME_UID="${PAGER_RUNTIME_UID:-1000}"
export PAGER_RUNTIME_GID="${PAGER_RUNTIME_GID:-1000}"

# Older gateway images ran as root and may therefore have created the SQLite,
# cursor and VAPID/session files as root inside an otherwise user-owned state
# directory. Reconcile only the known gateway-owned files when this helper itself
# is root. Refuse symlinks before chown/chmod so a compromised state directory
# cannot trick the privileged update helper into modifying an arbitrary host file.
if [[ "$EUID" -eq 0 && -d "$PAGER_DATA_HOST_PATH" ]]; then
  for name in pager.db pager.db-wal pager.db-shm pdl.log.racher-cursor session-secret vapid-private.pem; do
    path="$PAGER_DATA_HOST_PATH/$name"
    if [[ -L "$path" ]]; then
      echo "Afviser usikker symlink i pager-state: $path" >&2
      exit 1
    fi
    if [[ -f "$path" ]]; then
      chown "$PAGER_RUNTIME_UID:$PAGER_RUNTIME_GID" "$path"
      case "$name" in
        session-secret|vapid-private.pem) chmod 0600 "$path" ;;
        *) chmod 0640 "$path" ;;
      esac
    fi
  done
fi

if docker compose version >/dev/null 2>&1; then
  exec docker compose -f "$COMPOSE_FILE" "$@"
elif command -v docker-compose >/dev/null 2>&1; then
  exec docker-compose -f "$COMPOSE_FILE" "$@"
else
  echo "Docker Compose mangler." >&2
  exit 1
fi
